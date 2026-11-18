// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Workload identity — authenticate an agent by its own IdP-issued OIDC/SPIFFE
//! token instead of a shared API key.
//!
//! When a bearer token is neither a Brutor API key nor a valid gateway HMAC
//! JWT, this module attempts to verify it as an *external-IdP* token:
//!
//!   1. Read the unverified `iss` claim to select the tenant's matching
//!      `idp_servers` row (by issuer).
//!   2. Fetch + cache that IdP's JWKS (`jwks_uri`), select the signing key by
//!      `kid`, and verify the signature (RS256/384/512, PS256/384/512,
//!      ES256/384, EdDSA; never HMAC) plus `iss`, `exp` and `aud`. The
//!      audience must be one of the IdP's `workload_audiences`; an IdP with
//!      none configured authenticates no workloads (without it, a token
//!      the same IdP minted for any other application was accepted).
//!   3. Resolve the agent by `(idp_server_id, sub)` against `agent_identities`.
//!
//! The token's times: `iat` and `sub` are required; `nbf` is honoured; `iat`,
//! `nbf` and `exp` allow 60 s of clock skew; `exp - iat` is at most 3600 s
//! (an identity policy may require less; the identity judge holds that). A
//! key id the cached JWKS lacks forces at most one refetch per IdP per 30 s.
//! An issuer two enabled identity providers of the tenant share verifies
//! nothing (never one of them picked arbitrarily).
//!
//! Fail-closed: any verification, fetch, or lookup failure yields `None` (the
//! caller treats the token as not a workload identity and falls through to the
//! existing 401). The JWKS cache never caches a fetch error, so a transient IdP
//! outage doesn't pin a failure.

use std::sync::OnceLock;
use std::time::{Duration, Instant};

use base64::Engine as _;
use dashmap::DashMap;
use jsonwebtoken::jwk::{AlgorithmParameters, Jwk, JwkSet};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header};
use serde::Deserialize;
use sqlx::PgPool;

/// How long a fetched JWKS is trusted before re-fetch.
const JWKS_TTL: Duration = Duration::from_secs(300);

/// The shortest time between two forced refetches of one IdP's JWKS. A
/// token naming a key id the cached set does not have forces one refetch (a
/// key rotation); a burst of such tokens must not make the gateway fetch the
/// IdP's key set on every request. In between, an unknown key id fails
/// closed.
const JWKS_FORCED_REFETCH_INTERVAL: Duration = Duration::from_secs(30);

/// The longest a workload token may live (`exp - iat`), whatever any policy
/// says: a longer-lived token is a key with a signature.
pub const TOKEN_LIFETIME_CEILING_S: i64 = 3600;

/// Clock skew allowed on `iat`, `nbf` and `exp`.
pub const CLOCK_LEEWAY_S: u64 = 60;

/// The verified workload principal: which agent the token authenticates as,
/// plus the IdP coordinates for evidence.
#[derive(Debug, Clone)]
pub struct ResolvedAgent {
    pub agent_id: String,
    pub idp_server_id: String,
    pub idp_subject: String,
    /// The token's verified `software_id` /
    /// `software_version` / `software_build` claims, normalised. DESCRIBES
    /// the software; the agent is resolved by `(idp_server_id, sub)` alone
    /// and nothing here takes part in that.
    pub software: Option<crate::services::agent_impl::Release>,
    /// The verified token's lifetime, `exp - iat`, in seconds.
    pub token_lifetime_s: i64,
}

/// Minimal claims we read. Verification is done by `jsonwebtoken`; `sub` (the
/// workload subject) resolves the agent, and the RFC 7591 software claims
///  describe its release. The software claims are kept as raw JSON
/// so a non-string value is dropped by normalisation rather than failing the
/// whole decode (which would fail the authentication).
#[derive(Debug, Deserialize)]
struct Claims {
    sub: Option<String>,
    #[serde(default)]
    exp: Option<i64>,
    #[serde(default)]
    iat: Option<i64>,
    #[serde(default)]
    software_id: Option<serde_json::Value>,
    #[serde(default)]
    software_version: Option<serde_json::Value>,
    #[serde(default)]
    software_build: Option<serde_json::Value>,
}

impl Claims {
    fn software(&self) -> Option<crate::services::agent_impl::Release> {
        // A non-string claim is present but illegal: carried as a marker
        // the normaliser rejects (and counts), never stored.
        fn text(v: &Option<serde_json::Value>) -> Option<String> {
            v.as_ref().map(|v| match v {
                serde_json::Value::String(s) => s.clone(),
                other => format!("\u{fffd}{other}"),
            })
        }
        crate::services::agent_impl::release_from_claims(
            text(&self.software_id).as_deref(),
            text(&self.software_version).as_deref(),
            text(&self.software_build).as_deref(),
        )
    }
}

/// Process-global JWKS cache, keyed by idp_server_id.
static JWKS_CACHE: OnceLock<DashMap<String, (Instant, JwkSet)>> = OnceLock::new();
fn jwks_cache() -> &'static DashMap<String, (Instant, JwkSet)> {
    JWKS_CACHE.get_or_init(DashMap::new)
}

/// When each IdP's JWKS was last refetched because of an unknown key id.
static JWKS_FORCED: OnceLock<DashMap<String, Instant>> = OnceLock::new();

/// Whether a forced refetch of `idp_server_id`'s JWKS may run now; taking
/// the slot and checking it are one step, so concurrent misses fetch once.
fn take_forced_refetch_slot(idp_server_id: &str) -> bool {
    let forced = JWKS_FORCED.get_or_init(DashMap::new);
    let mut slot = forced
        .entry(idp_server_id.to_string())
        .or_insert_with(|| Instant::now() - JWKS_FORCED_REFETCH_INTERVAL);
    if slot.elapsed() < JWKS_FORCED_REFETCH_INTERVAL {
        return false;
    }
    *slot = Instant::now();
    true
}

/// Shared HTTP client for JWKS fetches (connect timeout bounded).
static HTTP: OnceLock<reqwest::Client> = OnceLock::new();
fn http() -> &'static reqwest::Client {
    HTTP.get_or_init(|| {
        crate::services::outbound_policy::client_builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap_or_default()
    })
}

/// Read the unverified `iss` claim from a JWT without validating the signature.
/// Used only to *select* which IdP to verify against — the full verified decode
/// follows. Returns `None` for a malformed token.
pub fn peek_issuer(token: &str) -> Option<String> {
    let payload_b64 = token.split('.').nth(1)?;
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(payload_b64)
        .ok()?;
    let v: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    v.get("iss")?.as_str().map(str::to_owned)
}

/// Verify `token` as an external-IdP workload token for `tenant_id` and resolve
/// the agent it authenticates as. Returns `Ok(None)` when the token is not a
/// (verifiable) workload identity for this tenant — the caller falls through.
pub async fn resolve_agent_from_token(
    db: &PgPool,
    tenant_id: &str,
    token: &str,
) -> Result<Option<ResolvedAgent>, sqlx::Error> {
    Ok(match resolve_workload_token(db, tenant_id, token).await? {
        WorkloadToken::Agent(agent) => Some(agent),
        WorkloadToken::FallenAgent { .. } | WorkloadToken::NoAgent => None,
    })
}

/// What a bearer token is, as a workload credential.
pub enum WorkloadToken {
    /// Verified, and the agent it names stands.
    Agent(ResolvedAgent),
    /// Verified, and it names an agent identity of this tenant that does
    /// not stand (disabled, revoked, expired, not `active`). No credential;
    /// the caller is told what `NoAgent` is told, and the refusal is
    /// recorded (`refused_agent_credential`).
    FallenAgent { agent_id: String },
    /// Not verifiable here, or it names no agent.
    NoAgent,
}

/// [`resolve_agent_from_token`], telling a fallen agent's token from one
/// that is nobody's.
pub async fn resolve_workload_token(
    db: &PgPool,
    tenant_id: &str,
    token: &str,
) -> Result<WorkloadToken, sqlx::Error> {
    // 1. Select the IdP by the token's (unverified) issuer.
    let Some(issuer) = peek_issuer(token) else {
        return Ok(WorkloadToken::NoAgent);
    };
    type IdpRow = (
        String,
        Option<String>,
        Option<sqlx::types::Json<serde_json::Value>>,
    );
    // Two enabled OIDC providers of the tenant with this issuer: which one
    // verifies the token, and so which agents its subject can name, would be
    // the database's arbitrary choice. Refused, never picked: the token is
    // not verifiable here until the operator removes the ambiguity.
    let mut idps: Vec<IdpRow> = sqlx::query_as(
        "SELECT id, jwks_uri, workload_audiences FROM idp_servers \
         WHERE tenant_id = $1 AND issuer = $2 AND enabled = true AND protocol = 'oidc' \
         ORDER BY id LIMIT 2",
    )
    .bind(tenant_id)
    .bind(&issuer)
    .fetch_all(db)
    .await?;
    if idps.len() > 1 {
        tracing::warn!(
            %tenant_id,
            %issuer,
            "workload token refused: two enabled identity providers of the tenant share its issuer"
        );
        return Ok(WorkloadToken::NoAgent);
    }
    let idp = idps.pop();
    let Some((idp_server_id, Some(jwks_uri), audiences)) = idp else {
        // No matching enabled IdP, or it has no JWKS endpoint → not verifiable.
        return Ok(WorkloadToken::NoAgent);
    };
    let audiences = workload_audiences(audiences.as_ref().map(|j| &j.0));
    if audiences.is_empty() {
        tracing::warn!(
            idp_server_id,
            "workload token refused: the IdP has no workload audiences configured"
        );
        return Ok(WorkloadToken::NoAgent);
    }

    // 2. Verify the signature + iss + aud + exp against the IdP's JWKS.
    let (subject, software, token_lifetime_s) =
        match verify_against_jwks(token, &idp_server_id, &jwks_uri, &issuer, &audiences).await {
            Some(verified) => verified,
            None => return Ok(WorkloadToken::NoAgent),
        };

    // 3. Resolve the agent by (idp_server_id, sub). Inactive/revoked agents
    //    don't resolve (the engine would hard-deny them anyway, but not
    //    resolving keeps a stale token from even attributing an actor).
    //    Neither does an identity past its `expires_at`:
    //    the token's own `exp` is the identity provider's statement
    //    about the token; `expires_at` is the operator's about the agent,
    //    and nothing else reads it on this path. Judged on the database's
    //    clock, in the statement that reads the row.
    //    An identity that stands is preferred, so a fallen row never hides
    //    a standing one with the same subject. NULL (a NULL column) is not
    //    standing.
    let agent: Option<(String, Option<bool>)> = sqlx::query_as(
        "SELECT id, \
                (enabled = true AND status = 'active' AND revoked_at IS NULL \
                 AND (expires_at IS NULL OR expires_at > now())) AS stands \
           FROM agent_identities \
          WHERE tenant_id = $1 AND idp_server_id = $2 AND idp_subject = $3 \
          ORDER BY stands DESC NULLS LAST LIMIT 1",
    )
    .bind(tenant_id)
    .bind(&idp_server_id)
    .bind(&subject)
    .fetch_optional(db)
    .await?;

    Ok(match agent {
        Some((agent_id, Some(true))) => WorkloadToken::Agent(ResolvedAgent {
            agent_id,
            idp_server_id,
            idp_subject: subject,
            software,
            token_lifetime_s,
        }),
        Some((agent_id, _)) => WorkloadToken::FallenAgent { agent_id },
        None => WorkloadToken::NoAgent,
    })
}

/// The configured audiences: trimmed, non-empty strings. Anything else in
/// the column (not a list, non-string entries) is ignored, so a malformed
/// value yields none and the IdP authenticates no workloads.
fn workload_audiences(raw: Option<&serde_json::Value>) -> Vec<String> {
    raw.and_then(|v| v.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

/// Signature algorithms a workload token may use. Asymmetric only: an HMAC
/// key in a JWKS is public, so anyone could sign with it.
const WORKLOAD_ALGORITHMS: &[Algorithm] = &[
    Algorithm::RS256,
    Algorithm::RS384,
    Algorithm::RS512,
    Algorithm::PS256,
    Algorithm::PS384,
    Algorithm::PS512,
    Algorithm::ES256,
    Algorithm::ES384,
    Algorithm::EdDSA,
];

/// Whether `jwk` may verify a token signed with `alg`: the key is not
/// symmetric, and when the IdP states the key's algorithm, the token uses
/// exactly that one (no RS256→PS256 or ES256→ES384 substitution).
fn key_accepts(jwk: &Jwk, alg: Algorithm) -> bool {
    if matches!(jwk.algorithm, AlgorithmParameters::OctetKey(_)) {
        return false;
    }
    if !WORKLOAD_ALGORITHMS.contains(&alg) {
        return false;
    }
    match jwk.common.key_algorithm {
        Some(stated) => format!("{stated:?}") == format!("{alg:?}"),
        None => true,
    }
}

/// Full verified decode: fetch/cache JWKS, select key by `kid`, verify
/// signature + issuer + audience + exp. Returns the `sub` claim and the
/// verified software claims on success, `None` on any failure (fail-closed).
async fn verify_against_jwks(
    token: &str,
    idp_server_id: &str,
    jwks_uri: &str,
    issuer: &str,
    audiences: &[String],
) -> Option<(String, Option<crate::services::agent_impl::Release>, i64)> {
    let header = decode_header(token).ok()?;
    let kid = header.kid?;

    // Try the cached JWKS first; on a kid miss (key rotation) force one
    // refetch, at most once per interval per IdP.
    let mut jwks = get_jwks(idp_server_id, jwks_uri, false).await?;
    if jwks.find(&kid).is_none() {
        if !take_forced_refetch_slot(idp_server_id) {
            tracing::debug!(
                idp_server_id,
                "workload token refused: unknown key id, and the key set was refetched moments ago"
            );
            return None;
        }
        jwks = get_jwks(idp_server_id, jwks_uri, true).await?;
    }
    let jwk = jwks.find(&kid)?;
    if !key_accepts(jwk, header.alg) {
        tracing::warn!(
            idp_server_id,
            alg = ?header.alg,
            "workload token refused: algorithm not allowed for this key"
        );
        return None;
    }
    let key = DecodingKey::from_jwk(jwk).ok()?;

    let mut validation = Validation::new(header.alg);
    validation.algorithms = vec![header.alg];
    validation.set_issuer(&[issuer]);
    validation.validate_exp = true;
    // `nbf` is honoured when present; `exp` and `nbf` with the clock leeway
    // (pinned, not the library's default).
    validation.validate_nbf = true;
    validation.leeway = CLOCK_LEEWAY_S;
    // `aud` must be present and match one of the IdP's workload audiences.
    // set_audience alone does not require it: jsonwebtoken 9 lets a token
    // with no `aud` through unless the claim is listed as required.
    validation.set_audience(audiences);
    validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);

    let data = decode::<Claims>(token, &key, &validation).ok()?;
    let lifetime = token_lifetime(data.claims.iat, data.claims.exp, now_secs()?)?;
    let software = data.claims.software();
    Some((data.claims.sub?, software, lifetime))
}

fn now_secs() -> Option<i64> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    i64::try_from(now.as_secs()).ok()
}

/// The token's lifetime (`exp - iat`) when it may authenticate at `now`:
/// `iat` present, not later than `now` plus the leeway, and the lifetime
/// positive and at most [`TOKEN_LIFETIME_CEILING_S`]. `None` refuses.
fn token_lifetime(iat: Option<i64>, exp: Option<i64>, now: i64) -> Option<i64> {
    let (iat, exp) = (iat?, exp?);
    let leeway = i64::try_from(CLOCK_LEEWAY_S).ok()?;
    if iat > now.saturating_add(leeway) {
        return None;
    }
    let lifetime = exp.checked_sub(iat)?;
    (lifetime > 0 && lifetime <= TOKEN_LIFETIME_CEILING_S).then_some(lifetime)
}

/// Get the IdP's JWKS, honoring the TTL cache. `force` bypasses the cache (used
/// once on a kid miss to pick up key rotation). A fetch error is never cached.
async fn get_jwks(idp_server_id: &str, jwks_uri: &str, force: bool) -> Option<JwkSet> {
    if !force {
        if let Some(entry) = jwks_cache().get(idp_server_id) {
            let (fetched_at, set) = entry.value();
            if fetched_at.elapsed() < JWKS_TTL {
                return Some(set.clone());
            }
        }
    }
    // `jwks_uri` is tenant-configured; an IP literal the outbound
    // policy denies is never fetched (the resolver never sees it).
    if let Err(denied) = crate::services::outbound_policy::check_url(jwks_uri) {
        tracing::warn!(idp_server_id, host = %denied.host, reason = denied.reason, "JWKS URI refused by outbound policy");
        return None;
    }
    let resp = http().get(jwks_uri).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let set: JwkSet = resp.json().await.ok()?;
    jwks_cache().insert(idp_server_id.to_string(), (Instant::now(), set.clone()));
    Some(set)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peek_issuer_reads_unverified_iss() {
        // header.payload.sig — payload is base64url(JSON) with an iss claim.
        let payload = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(br#"{"iss":"https://idp.example/","sub":"spiffe://acme/agent/1"}"#);
        let token = format!("aaaa.{}.bbbb", payload);
        assert_eq!(peek_issuer(&token).as_deref(), Some("https://idp.example/"));
    }

    #[test]
    fn token_lifetime_rules() {
        let now = 1_000_000;
        assert_eq!(token_lifetime(Some(now), Some(now + 3600), now), Some(3600));
        assert_eq!(token_lifetime(Some(now), Some(now + 3601), now), None);
        assert_eq!(token_lifetime(None, Some(now + 600), now), None);
        assert_eq!(
            token_lifetime(Some(now + 60), Some(now + 600), now),
            Some(540)
        );
        assert_eq!(token_lifetime(Some(now + 61), Some(now + 600), now), None);
        assert_eq!(token_lifetime(Some(now), Some(now), now), None);
        assert_eq!(token_lifetime(Some(now), Some(now - 10), now), None);
    }

    #[test]
    fn peek_issuer_none_for_garbage() {
        assert!(peek_issuer("not-a-jwt").is_none());
        assert!(peek_issuer("a.b").is_none()); // missing payload segment index 1 is "b"…
    }
}
