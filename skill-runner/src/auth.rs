// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! JWT verification for the runner's `/execute` endpoint.
//!
//! The proxy mints a short-lived HS256 token for every execution.
//! The runner verifies the signature with the shared secret and
//! asserts that the `exec` claim matches the `execution_id` in the
//! request body. This stops a single token from being replayed
//! against a different execution and from being reused after its
//! 5-minute window.
//!
//! The runner only ever VERIFIES tokens; it never mints them. The
//! signing key is held by the proxy only; if the runner is
//! compromised, the attacker cannot mint new tokens because nothing
//! in the runner is authorised to sign anything.

use jsonwebtoken::{DecodingKey, Validation, decode};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct Claims {
    /// Execution ID. MUST match the request body's `execution_id`.
    pub exec: String,
    /// Tenant ID (informational; logged for correlation).
    pub tnt: String,
    /// Skill ID (informational).
    pub skl: String,
    /// Expiry — standard JWT field, enforced by jsonwebtoken with
    /// configured leeway.
    pub exp: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    #[error("missing Authorization header")]
    MissingHeader,
    #[error("authorization header is not Bearer")]
    NotBearer,
    #[error("invalid token: {0}")]
    Invalid(String),
    #[error("execution_id mismatch between token and request body")]
    ExecutionMismatch,
}

/// Verify a Bearer token from an `Authorization` header and return
/// its claims. Validates signature, expiry (with configured leeway),
/// and that `exec` matches `expected_execution_id`.
///
/// The bound `expected_execution_id` check is what closes the
/// replay window — without it, a leaked token could be re-used for
/// up to 5 minutes against any execution the attacker can guess
/// the ID of.
pub fn verify_bearer(
    header_value: &str,
    secret: &str,
    leeway_secs: u64,
    expected_execution_id: &str,
) -> Result<Claims, AuthError> {
    let token = header_value
        .strip_prefix("Bearer ")
        .ok_or(AuthError::NotBearer)?;

    let mut validation = Validation::new(jsonwebtoken::Algorithm::HS256);
    validation.leeway = leeway_secs;
    validation.set_required_spec_claims(&["exp"]);

    let token_data = decode::<Claims>(
        token,
        &DecodingKey::from_secret(secret.as_bytes()),
        &validation,
    )
    .map_err(|e| AuthError::Invalid(e.to_string()))?;

    if token_data.claims.exec != expected_execution_id {
        return Err(AuthError::ExecutionMismatch);
    }

    Ok(token_data.claims)
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{EncodingKey, Header, encode};
    use serde::Serialize;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    #[derive(Serialize)]
    struct TestClaims {
        exec: String,
        tnt: String,
        skl: String,
        exp: u64,
    }

    fn mint(secret: &str, claims: &TestClaims) -> String {
        encode(
            &Header::default(),
            claims,
            &EncodingKey::from_secret(secret.as_bytes()),
        )
        .unwrap()
    }

    const SECRET: &str = "01234567890123456789012345678901"; // 32 chars

    #[test]
    fn valid_token_passes() {
        let token = mint(
            SECRET,
            &TestClaims {
                exec: "exec-1".into(),
                tnt: "t-1".into(),
                skl: "skl-1".into(),
                exp: now() + 60,
            },
        );
        let header = format!("Bearer {token}");
        let claims = verify_bearer(&header, SECRET, 30, "exec-1").unwrap();
        assert_eq!(claims.exec, "exec-1");
        assert_eq!(claims.tnt, "t-1");
    }

    #[test]
    fn execution_mismatch_rejected() {
        let token = mint(
            SECRET,
            &TestClaims {
                exec: "exec-1".into(),
                tnt: "t-1".into(),
                skl: "skl-1".into(),
                exp: now() + 60,
            },
        );
        let header = format!("Bearer {token}");
        let err = verify_bearer(&header, SECRET, 30, "exec-2").unwrap_err();
        matches!(err, AuthError::ExecutionMismatch);
    }

    #[test]
    fn expired_token_rejected() {
        let token = mint(
            SECRET,
            &TestClaims {
                exec: "exec-1".into(),
                tnt: "t-1".into(),
                skl: "skl-1".into(),
                exp: now() - 60, // expired a minute ago
            },
        );
        let header = format!("Bearer {token}");
        assert!(matches!(
            verify_bearer(&header, SECRET, 30, "exec-1").unwrap_err(),
            AuthError::Invalid(_)
        ));
    }

    #[test]
    fn tampered_signature_rejected() {
        let token = mint(
            SECRET,
            &TestClaims {
                exec: "exec-1".into(),
                tnt: "t-1".into(),
                skl: "skl-1".into(),
                exp: now() + 60,
            },
        );
        let mut bad = token.clone();
        // Mutate the last char of the signature segment.
        let last = bad.pop().unwrap();
        let mutated = if last == 'A' { 'B' } else { 'A' };
        bad.push(mutated);
        let header = format!("Bearer {bad}");
        assert!(matches!(
            verify_bearer(&header, SECRET, 30, "exec-1").unwrap_err(),
            AuthError::Invalid(_)
        ));
    }

    #[test]
    fn missing_bearer_prefix_rejected() {
        assert!(matches!(
            verify_bearer("token-without-bearer", SECRET, 30, "exec-1").unwrap_err(),
            AuthError::NotBearer
        ));
    }
}
