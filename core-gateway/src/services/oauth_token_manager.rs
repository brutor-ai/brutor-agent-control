// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # OAuth Token Manager Service
//!
//! The security heart of the gateway. Manages the encryption, storage, and
//! proactive refreshing of sensitive upstream credentials.
//!
//! ### Core Logic:
//! - **Thundering Herd Protection**: Uses per-token async Mutexes to ensure only
//!   one thread refreshes an expired token at a time.
//! - **At-Rest Encryption**: Encrypts Refresh Tokens and Client Secrets before
//!   writing to Postgres using the `EncryptionService`.
//! - **Token Swapping**: Mints internal "McpJwt" tokens, keeping raw upstream
//!   access tokens hidden from client-side environments.
//! - **Lifecycle Hooks**: Handles auto-revocation if an IdP returns a 401 Unauthorized.

use std::collections::HashMap;
use std::sync::Arc;

use chrono::{Duration, Utc};
use jsonwebtoken::{DecodingKey, EncodingKey, Header, Validation, decode, encode};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use tokio::sync::Mutex;

use crate::models::db::{IdpServer, OAuthToken};
use crate::services::encryption::EncryptionService;
use crate::services::system_alerts::{SystemAlertInput, spawn_raise};

const JWT_ISSUER: &str = "brutor-agent-control";

/// MCP JWT claims — the token returned to clients after OAuth exchange.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpJwtClaims {
    pub sub: String,
    pub iat: i64,
    pub exp: i64,
    pub iss: String,
    pub jti: String,
    pub tenant_id: String,
    pub user_id: String,
    pub mcp_server_id: String,
    /// The identity provider whose subject `sub` is.
    /// A subject is only unique within its IdP, so the stored credential this
    /// JWT stands for is the one of this IdP and no other. Required: a JWT
    /// minted before the claim existed does not deserialize and is refused.
    pub idp_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(rename = "type")]
    pub token_type: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct UserInfo {
    pub user_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

pub struct OAuthTokenManager {
    encryption: EncryptionService,
    jwt_secret: String,
    jwt_algorithm: String,
    jwt_expires_in: i64,
    http_client: reqwest::Client,
    /// Per-token refresh locks to prevent concurrent refreshes.
    refresh_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl OAuthTokenManager {
    pub fn new(
        encryption: EncryptionService,
        jwt_secret: String,
        jwt_algorithm: String,
        jwt_expires_in: i64,
        http_client: reqwest::Client,
    ) -> Self {
        Self {
            encryption,
            jwt_secret,
            jwt_algorithm,
            jwt_expires_in,
            http_client,
            refresh_locks: Mutex::new(HashMap::new()),
        }
    }

    // -----------------------------------------------------------------------
    // MCP JWT creation & verification
    // -----------------------------------------------------------------------

    /// Create an MCP JWT for a client after successful OAuth exchange.
    pub fn create_mcp_jwt(
        &self,
        tenant_id: &str,
        idp_user_id: &str,
        mcp_server_id: &str,
        idp_id: &str,
        username: Option<&str>,
        email: Option<&str>,
        name: Option<&str>,
    ) -> Result<String, OAuthError> {
        let now = Utc::now();
        let claims = McpJwtClaims {
            sub: idp_user_id.to_string(),
            iat: now.timestamp(),
            exp: (now + Duration::seconds(self.jwt_expires_in)).timestamp(),
            iss: JWT_ISSUER.to_string(),
            jti: uuid::Uuid::new_v4().to_string(),
            tenant_id: tenant_id.to_string(),
            user_id: idp_user_id.to_string(),
            mcp_server_id: mcp_server_id.to_string(),
            idp_id: idp_id.to_string(),
            username: username.map(String::from),
            email: email.map(String::from),
            name: name.map(String::from),
            token_type: "mcp_auth".to_string(),
        };

        let header = Header::new(match self.jwt_algorithm.as_str() {
            "HS384" => jsonwebtoken::Algorithm::HS384,
            "HS512" => jsonwebtoken::Algorithm::HS512,
            _ => jsonwebtoken::Algorithm::HS256,
        });

        encode(
            &header,
            &claims,
            &EncodingKey::from_secret(self.jwt_secret.as_bytes()),
        )
        .map_err(|e| OAuthError::JwtError(e.to_string()))
    }

    /// Verify an MCP JWT and return the claims.
    pub fn verify_mcp_jwt(&self, token: &str) -> Result<McpJwtClaims, OAuthError> {
        let mut validation = Validation::new(match self.jwt_algorithm.as_str() {
            "HS384" => jsonwebtoken::Algorithm::HS384,
            "HS512" => jsonwebtoken::Algorithm::HS512,
            _ => jsonwebtoken::Algorithm::HS256,
        });
        validation.set_issuer(&[JWT_ISSUER]);

        let data = decode::<McpJwtClaims>(
            token,
            &DecodingKey::from_secret(self.jwt_secret.as_bytes()),
            &validation,
        )
        .map_err(|e| OAuthError::JwtError(e.to_string()))?;

        if data.claims.token_type != "mcp_auth" {
            return Err(OAuthError::JwtError("Invalid token type".into()));
        }

        Ok(data.claims)
    }

    // -----------------------------------------------------------------------
    // Authorization code exchange
    // -----------------------------------------------------------------------

    /// Exchange an authorization code for tokens with the IdP.
    pub async fn exchange_authorization_code(
        &self,
        idp: &IdpServer,
        code: &str,
        client_id: &str,
        redirect_uri: Option<&str>,
        client_secret: Option<&str>,
        code_verifier: Option<&str>,
    ) -> Result<ExtractedTokens, OAuthError> {
        check_idp_destinations(idp).map_err(|d| OAuthError::ConfigError(d.to_string()))?;
        let token_endpoint = idp
            .token_endpoint
            .as_deref()
            .ok_or_else(|| OAuthError::ConfigError("IdP has no token_endpoint".into()))?;

        let mut form = HashMap::new();
        form.insert("grant_type", "authorization_code");
        form.insert("code", code);
        form.insert("client_id", client_id);
        if let Some(uri) = redirect_uri {
            form.insert("redirect_uri", uri);
        }
        if let Some(secret) = client_secret {
            form.insert("client_secret", secret);
        }
        if let Some(verifier) = code_verifier {
            form.insert("code_verifier", verifier);
        }

        let resp = self
            .http_client
            .post(token_endpoint)
            .form(&form)
            .timeout(std::time::Duration::from_secs(30))
            .send()
            .await
            .map_err(|e| OAuthError::ExchangeFailed(format!("HTTP error: {e}")))?;

        let status = resp.status();
        let body_text = resp
            .text()
            .await
            .map_err(|e| OAuthError::ExchangeFailed(format!("Read body: {e}")))?;

        if !status.is_success() {
            return Err(OAuthError::ExchangeFailed(format!(
                "IdP returned {status}: {body_text}"
            )));
        }

        self.parse_and_extract_tokens(&body_text, "authorization_code", None)
    }

    /// Refresh an existing token.
    pub async fn refresh_token_with_idp(
        &self,
        idp: &IdpServer,
        refresh_token: &str,
        client_id: &str,
        client_secret: Option<&str>,
    ) -> Result<ExtractedTokens, OAuthError> {
        check_idp_destinations(idp).map_err(|d| OAuthError::ConfigError(d.to_string()))?;
        let token_endpoint = idp
            .token_endpoint
            .as_deref()
            .ok_or_else(|| OAuthError::ConfigError("IdP has no token_endpoint".into()))?;

        let mut form = HashMap::new();
        form.insert("grant_type", "refresh_token");
        form.insert("refresh_token", refresh_token);
        form.insert("client_id", client_id);
        if let Some(secret) = client_secret {
            form.insert("client_secret", secret);
        }

        let resp = self
            .http_client
            .post(token_endpoint)
            .form(&form)
            .timeout(std::time::Duration::from_secs(30))
            .send()
            .await
            .map_err(|e| OAuthError::RefreshFailed(format!("HTTP error: {e}")))?;

        let status = resp.status();
        if status.as_u16() == 401 {
            return Err(OAuthError::TokenRevoked);
        }

        let body_text = resp
            .text()
            .await
            .map_err(|e| OAuthError::RefreshFailed(format!("Read body: {e}")))?;

        if !status.is_success() {
            return Err(OAuthError::RefreshFailed(format!(
                "IdP returned {status}: {body_text}"
            )));
        }

        self.parse_and_extract_tokens(&body_text, "refresh_token", Some(refresh_token))
    }

    fn parse_and_extract_tokens(
        &self,
        body: &str,
        _grant_type: &str,
        fallback_refresh: Option<&str>,
    ) -> Result<ExtractedTokens, OAuthError> {
        // Try JSON first, then form-encoded
        let parsed: serde_json::Value = serde_json::from_str(body)
            .or_else(|_| {
                let pairs: HashMap<String, String> =
                    serde_urlencoded::from_str(body).unwrap_or_default();
                serde_json::to_value(pairs)
                    .map_err(|e| OAuthError::ExchangeFailed(format!("Parse: {e}")))
            })
            .map_err(|_| {
                OAuthError::ExchangeFailed(format!("Cannot parse token response: {body}"))
            })?;

        let access_token = parsed
            .get("access_token")
            .and_then(|v| v.as_str())
            .ok_or_else(|| OAuthError::ExchangeFailed("No access_token in response".into()))?
            .to_string();

        let refresh_token = parsed
            .get("refresh_token")
            .and_then(|v| v.as_str())
            .map(String::from)
            .or_else(|| fallback_refresh.map(String::from));

        let expires_in = parsed
            .get("expires_in")
            .and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            })
            .unwrap_or(3600);

        let token_type = parsed
            .get("token_type")
            .and_then(|v| v.as_str())
            .unwrap_or("Bearer")
            .to_string();

        let scope = parsed
            .get("scope")
            .and_then(|v| v.as_str())
            .map(String::from);

        Ok(ExtractedTokens {
            access_token,
            refresh_token,
            expires_in,
            token_type,
            scope,
        })
    }

    // -----------------------------------------------------------------------
    // Token storage
    // -----------------------------------------------------------------------

    /// Encrypt and store OAuth tokens in the database.
    /// Upserts on the table's unique key, (tenant_id, idp_id, idp_user_id,
    /// mcp_server_id).
    ///
    /// A subject is unique within its identity provider only, and the key
    /// says so: two identity providers of one tenant that both name a subject
    /// `42` each hold their own row for the same server. A code exchange
    /// therefore meets only its own identity provider's earlier credential
    /// for that subject and server (live or revoked) and renews it: a new
    /// authorization, so the row is live again, and what the identity
    /// provider did not send again (refresh token, client, issuer) is kept.
    /// Another identity provider's row is never read or written here.
    pub async fn store_token(
        &self,
        db: &PgPool,
        gateway_user_id: Option<&str>,
        idp_user_id: &str,
        mcp_server_id: &str,
        tenant_id: &str,
        idp_id: &str,
        tokens: &ExtractedTokens,
        client_id: Option<&str>,
        client_secret: Option<&str>,
        issuer: Option<&str>,
    ) -> Result<(), OAuthError> {
        let encrypted_access = self
            .encryption
            .encrypt(&tokens.access_token)
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;

        let encrypted_refresh = tokens
            .refresh_token
            .as_deref()
            .map(|t| self.encryption.encrypt(t))
            .transpose()
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;

        let encrypted_secret = client_secret
            .filter(|s| !s.is_empty())
            .map(|s| self.encryption.encrypt(s))
            .transpose()
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;

        let expires_at = (Utc::now() + Duration::seconds(tokens.expires_in)).naive_utc();
        let now = Utc::now().naive_utc();

        // Upsert: INSERT ... ON CONFLICT UPDATE
        sqlx::query(
            r#"INSERT INTO oauth_tokens (
                tenant_id, gateway_user_id, idp_user_id, mcp_server_id, idp_id,
                client_id, encrypted_client_secret,
                encrypted_access_token, encrypted_refresh_token,
                token_type, expires_at, scopes,
                revoked, created_at, refresh_count, updated_at, issuer
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, false, $13, 0, $13, $14)
            ON CONFLICT (tenant_id, idp_id, idp_user_id, mcp_server_id)
            DO UPDATE SET
                issuer = COALESCE(EXCLUDED.issuer, oauth_tokens.issuer),
                gateway_user_id = EXCLUDED.gateway_user_id,
                encrypted_access_token = EXCLUDED.encrypted_access_token,
                encrypted_refresh_token = COALESCE(EXCLUDED.encrypted_refresh_token, oauth_tokens.encrypted_refresh_token),
                encrypted_client_secret = COALESCE(EXCLUDED.encrypted_client_secret, oauth_tokens.encrypted_client_secret),
                client_id = COALESCE(EXCLUDED.client_id, oauth_tokens.client_id),
                token_type = EXCLUDED.token_type,
                expires_at = EXCLUDED.expires_at,
                scopes = EXCLUDED.scopes,
                revoked = false,
                revoked_at = NULL,
                last_refreshed_at = $13,
                refresh_count = oauth_tokens.refresh_count + 1,
                updated_at = $13
            "#,
        )
        .bind(tenant_id)
        .bind(gateway_user_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .bind(client_id)
        .bind(&encrypted_secret)
        .bind(&encrypted_access)
        .bind(&encrypted_refresh)
        .bind(&tokens.token_type)
        .bind(expires_at)
        .bind(tokens.scope.as_deref())
        .bind(now)
        .bind(issuer)
        .execute(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?;

        Ok(())
    }

    // -----------------------------------------------------------------------
    // Token retrieval with auto-refresh
    // -----------------------------------------------------------------------

    /// Get a valid (decrypted) access token, refreshing if expired.
    /// Returns None if no token exists or refresh fails.
    ///
    /// `idp_id` is the IdP whose subject `idp_user_id` is:
    /// a row of another IdP is another person's credential, whatever
    /// its subject string, and is neither served nor refreshed.
    pub async fn get_valid_token(
        &self,
        db: &PgPool,
        gateway_user_id: Option<&str>,
        idp_user_id: &str,
        mcp_server_id: &str,
        tenant_id: &str,
        idp_id: &str,
        auto_refresh: bool,
    ) -> Option<String> {
        let token: OAuthToken = match sqlx::query_as(
            r#"SELECT * FROM oauth_tokens
               WHERE tenant_id = $1 AND idp_user_id = $2 AND mcp_server_id = $3
                 AND idp_id = $4 AND revoked = false
               ORDER BY created_at DESC LIMIT 1"#,
        )
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .fetch_optional(db)
        .await
        {
            Ok(Some(t)) => t,
            Ok(None) => {
                tracing::debug!(
                    tenant_id,
                    idp_user_id,
                    mcp_server_id,
                    "No OAuth token found in DB"
                );
                return None;
            }
            Err(e) => {
                tracing::error!(tenant_id, idp_user_id, mcp_server_id, error = %e, "Failed to query OAuth token");
                return None;
            }
        };

        // Valid token — decrypt and return
        if token.is_valid() {
            return self.encryption.decrypt(&token.encrypted_access_token).ok();
        }

        // Expired but we can't refresh
        if !auto_refresh || token.encrypted_refresh_token.is_none() {
            return None;
        }

        // Refresh with per-token lock to prevent concurrent refreshes
        let lock_key = format!("{tenant_id}:{idp_id}:{idp_user_id}:{mcp_server_id}");
        let lock = {
            let mut locks = self.refresh_locks.lock().await;
            locks
                .entry(lock_key.clone())
                .or_insert_with(|| Arc::new(Mutex::new(())))
                .clone()
        };

        let _guard = lock.lock().await;

        // Re-check after acquiring lock — another task may have refreshed
        let token: OAuthToken = match sqlx::query_as(
            r#"SELECT * FROM oauth_tokens
               WHERE tenant_id = $1 AND idp_user_id = $2 AND mcp_server_id = $3
                 AND idp_id = $4 AND revoked = false
               ORDER BY created_at DESC LIMIT 1"#,
        )
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .fetch_optional(db)
        .await
        {
            Ok(Some(t)) => t,
            Ok(None) => return None,
            Err(e) => {
                tracing::error!(error = %e, "Failed to re-query OAuth token after lock");
                return None;
            }
        };

        if token.is_valid() {
            return self.encryption.decrypt(&token.encrypted_access_token).ok();
        }

        // Actually refresh
        let result = self
            .refresh_stored_token(db, &token, gateway_user_id, tenant_id)
            .await;

        // Clean up lock
        {
            let mut locks = self.refresh_locks.lock().await;
            locks.remove(&lock_key);
        }

        match result {
            Ok(new_access_token) => Some(new_access_token),
            Err(e) => {
                // Token refresh failure means the IdP either rejected
                // our refresh token (user needs to re-auth) or is itself
                // unreachable. Raise a downstream_idp alert scoped to
                // the IdP id so repeated failures against the same IdP
                // coalesce — a configuration break against Okta would
                // otherwise raise N alerts for N users.
                tracing::warn!("Token refresh failed for {lock_key}: {e}");
                spawn_raise(
                    db.clone(),
                    SystemAlertInput::downstream_idp(
                        Some(tenant_id.to_string()),
                        &token.idp_id,
                        None,
                        Some(classify_oauth_error(&e)),
                        format!("OAuth token refresh failed: {e}"),
                    ),
                );
                None
            }
        }
    }

    /// Record (best-effort) that an agent principal exercised this user-held
    /// token via the secretless broker — the broker-side half of the
    /// on-behalf-of evidence (the per-request `proxy_logs.actor_agent_id` is the
    /// other half). Keyed identically to the token lookup. Never fails the
    /// request: a failed UPDATE is logged and swallowed.
    pub async fn record_agent_use(
        &self,
        db: &PgPool,
        tenant_id: &str,
        idp_user_id: &str,
        mcp_server_id: &str,
        idp_id: &str,
        agent_id: &str,
    ) {
        let res = sqlx::query(
            "UPDATE oauth_tokens \
             SET last_actor_agent_id = $1, last_used_by_agent_at = now() \
             WHERE tenant_id = $2 AND idp_user_id = $3 AND mcp_server_id = $4 \
               AND idp_id = $5",
        )
        .bind(agent_id)
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .execute(db)
        .await;
        if let Err(e) = res {
            tracing::warn!(error = %e, agent = %agent_id, "failed to record agent use on oauth token");
        }
    }

    /// Revoke the row a refresh read.
    ///
    /// Keyed on the row `id`, which names one credential of one identity
    /// provider for good: a row never changes its identity provider. The
    /// statement names the identity provider it read as well, so that it
    /// holds by its own predicate and not by what other writers of the table
    /// do: a row that is not the read credential's is never revoked by id.
    async fn revoke_read_row(&self, db: &PgPool, token: &OAuthToken) {
        let res = sqlx::query(
            "UPDATE oauth_tokens SET revoked = true, revoked_at = NOW(), updated_at = NOW() \
             WHERE id = $1 AND idp_id = $2 AND revoked = false",
        )
        .bind(token.id)
        .bind(&token.idp_id)
        .execute(db)
        .await;
        if let Err(e) = res {
            tracing::error!(token_id = token.id, error = %e, "failed to revoke an OAuth token");
        }
    }

    /// Write a refresh grant's result into the row it was made for.
    ///
    /// The refresh does not write back through `store_token`,
    /// the code-exchange upsert, which is a NEW authorization and so sets
    /// `revoked = false`: a credential revoked while its IdP was answering
    /// the refresh would come back to life with a fresh access and refresh token.
    /// A refresh is not an authorization: it updates the row it read, only
    /// while that row is still live (and still its identity provider's, as in
    /// `revoke_read_row`), and otherwise writes nothing and fails as revoked
    /// (the caller serves no token).
    async fn write_refreshed(
        &self,
        db: &PgPool,
        token: &OAuthToken,
        gateway_user_id: Option<&str>,
        tokens: &ExtractedTokens,
        issuer: &str,
    ) -> Result<(), OAuthError> {
        let encrypted_access = self
            .encryption
            .encrypt(&tokens.access_token)
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;
        let encrypted_refresh = tokens
            .refresh_token
            .as_deref()
            .map(|t| self.encryption.encrypt(t))
            .transpose()
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;
        let now = Utc::now().naive_utc();
        let expires_at = (Utc::now() + Duration::seconds(tokens.expires_in)).naive_utc();

        let written = sqlx::query(
            r#"UPDATE oauth_tokens SET
                   issuer = $3,
                   gateway_user_id = $4,
                   encrypted_access_token = $5,
                   encrypted_refresh_token = COALESCE($6, encrypted_refresh_token),
                   token_type = $7,
                   expires_at = $8,
                   scopes = $9,
                   last_refreshed_at = $10,
                   refresh_count = refresh_count + 1,
                   updated_at = $10
               WHERE id = $1 AND idp_id = $2 AND revoked = false"#,
        )
        .bind(token.id)
        .bind(&token.idp_id)
        .bind(issuer)
        .bind(gateway_user_id)
        .bind(&encrypted_access)
        .bind(&encrypted_refresh)
        .bind(&tokens.token_type)
        .bind(expires_at)
        .bind(&tokens.scope)
        .bind(now)
        .execute(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?;

        if written.rows_affected() == 0 {
            tracing::warn!(
                token_id = token.id,
                "OAuth refresh result discarded: the credential was revoked while the \
                 identity provider was answering"
            );
            return Err(OAuthError::TokenRevoked);
        }
        Ok(())
    }

    /// Refresh a stored token using its refresh_token.
    async fn refresh_stored_token(
        &self,
        db: &PgPool,
        token: &OAuthToken,
        gateway_user_id: Option<&str>,
        tenant_id: &str,
    ) -> Result<String, OAuthError> {
        let encrypted_refresh = token
            .encrypted_refresh_token
            .as_deref()
            .ok_or_else(|| OAuthError::RefreshFailed("No refresh token".into()))?;

        let refresh_token = self
            .encryption
            .decrypt(encrypted_refresh)
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;

        // Get IdP config
        let idp: IdpServer = sqlx::query_as(
            "SELECT * FROM idp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true \
             AND protocol = 'oidc'",
        )
        .bind(&token.idp_id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?
        .ok_or_else(|| OAuthError::ConfigError("IdP not found".into()))?;

        // Decrypt client_secret if stored
        let client_secret = token
            .encrypted_client_secret
            .as_deref()
            .map(|s| self.encryption.decrypt(s))
            .transpose()
            .map_err(|e| OAuthError::EncryptionError(e.to_string()))?;

        let client_id = token.client_id.as_deref().unwrap_or("");

        // SEP-2352: issuer binding: a token minted
        // under one authorization server must never be refreshed
        // against another. If the IdP's issuer changed since mint,
        // revoke and force a fresh authorization.
        if let Some(stored_iss) = token.issuer.as_deref() {
            if stored_iss != idp.issuer {
                tracing::warn!(
                    token_id = token.id,
                    stored_issuer = %stored_iss,
                    current_issuer = %idp.issuer,
                    "OAuth token issuer changed since mint — revoking (re-authorization required)"
                );
                self.revoke_read_row(db, token).await;
                return Err(OAuthError::RefreshFailed(
                    "Authorization server issuer changed — re-authorization required".into(),
                ));
            }
        }

        let new_tokens = self
            .refresh_token_with_idp(&idp, &refresh_token, client_id, client_secret.as_deref())
            .await;

        match new_tokens {
            Ok(tokens) => {
                let new_access = tokens.access_token.clone();
                self.write_refreshed(db, token, gateway_user_id, &tokens, &idp.issuer)
                    .await?;
                Ok(new_access)
            }
            Err(OAuthError::TokenRevoked) => {
                // Mark as revoked in DB
                self.revoke_read_row(db, token).await;
                Err(OAuthError::TokenRevoked)
            }
            Err(e) => Err(e),
        }
    }

    // -----------------------------------------------------------------------
    // User info from IdP
    // -----------------------------------------------------------------------

    /// Fetch user info from the IdP's userinfo endpoint.
    pub async fn get_user_info(
        &self,
        access_token: &str,
        userinfo_endpoint: &str,
    ) -> Result<UserInfo, OAuthError> {
        // HubSpot special case: endpoint contains {token} placeholder
        let url = if userinfo_endpoint.contains("{token}") {
            userinfo_endpoint.replace("{token}", access_token)
        } else {
            userinfo_endpoint.to_string()
        };
        // Tenant-configured endpoint; refuse a denied IP literal.
        crate::services::outbound_policy::check_url(&url)
            .map_err(|d| OAuthError::UserInfoFailed(d.to_string()))?;

        let mut req = self
            .http_client
            .get(&url)
            .header("User-Agent", "BrutorAI-Proxy/1.0")
            .header("Accept", "application/json");
        if !userinfo_endpoint.contains("{token}") {
            req = req.header("Authorization", format!("Bearer {access_token}"));
        }

        let resp = req
            .timeout(std::time::Duration::from_secs(15))
            .send()
            .await
            // `without_url`: the URL can hold the access token ({token} above),
            // and this error is logged.
            .map_err(|e| OAuthError::UserInfoFailed(format!("HTTP: {}", e.without_url())))?;

        if !resp.status().is_success() {
            return Err(OAuthError::UserInfoFailed(format!(
                "IdP returned {}",
                resp.status()
            )));
        }

        let info: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| OAuthError::UserInfoFailed(format!("Parse: {}", e.without_url())))?;

        // Extract user_id with priority chain. An empty string is no subject:
        // stored under it, every such user would share one credential.
        let user_id = ["sub", "id", "id_str", "user", "user_id", "userid", "hub_id"]
            .iter()
            .find_map(|key| {
                info.get(key).and_then(|v| {
                    v.as_str()
                        .filter(|s| !s.is_empty())
                        .map(String::from)
                        .or_else(|| v.as_i64().map(|n| n.to_string()))
                        .or_else(|| v.as_u64().map(|n| n.to_string()))
                })
            })
            .ok_or_else(|| OAuthError::UserInfoFailed("No user_id in response".into()))?;

        let username = info
            .get("login")
            .or_else(|| info.get("preferred_username"))
            .or_else(|| info.get("username"))
            .or_else(|| info.get("user"))
            .and_then(|v| v.as_str())
            .map(String::from);

        let email = info.get("email").and_then(|v| v.as_str()).map(String::from);

        let name = info.get("name").and_then(|v| v.as_str()).map(String::from);

        Ok(UserInfo {
            user_id,
            username,
            email,
            name,
        })
    }

    // -----------------------------------------------------------------------
    // Idempotency
    // -----------------------------------------------------------------------

    /// Create an idempotency key for token exchange deduplication.
    ///
    /// `proof` is what the caller proved the code with (its PKCE verifier or
    /// client secret). A retry carries the same proof and finds the cached
    /// answer, which is a live JWT; an intercepted code with any other proof
    /// misses it and has to convince the IdP.
    pub fn create_idempotency_key(
        idp_id: &str,
        code: &str,
        mcp_server_id: &str,
        client_id: &str,
        proof: Option<&str>,
    ) -> String {
        let proof = proof.unwrap_or("");
        let input = format!("{idp_id}:{code}:{mcp_server_id}:{client_id}:{proof}");
        let hash = hex::encode(Sha256::digest(input.as_bytes()));
        format!("token_exchange:{}", &hash[..16])
    }

    /// Check idempotency state in Redis. Returns:
    /// - Ok(None) → proceed with exchange
    /// - Ok(Some(result)) → return cached result
    /// - Err → duplicate in progress (409)
    pub async fn check_idempotency(
        redis: &mut redis::aio::ConnectionManager,
        key: &str,
    ) -> Result<Option<serde_json::Value>, OAuthError> {
        let value: Option<String> = redis::cmd("GET")
            .arg(key)
            .query_async(redis)
            .await
            .unwrap_or(None);

        match value {
            None => {
                // Mark as processing with 60s TTL
                let _: () = redis::cmd("SET")
                    .arg(key)
                    .arg(r#"{"status":"processing"}"#)
                    .arg("EX")
                    .arg(60)
                    .query_async(redis)
                    .await
                    .unwrap_or(());
                Ok(None)
            }
            Some(stored) => {
                let parsed: serde_json::Value = serde_json::from_str(&stored).unwrap_or_default();
                let status = parsed.get("status").and_then(|v| v.as_str()).unwrap_or("");

                match status {
                    "processing" => Err(OAuthError::DuplicateExchange),
                    "completed" => {
                        let result = parsed.get("result").cloned();
                        Ok(result)
                    }
                    _ => Ok(None),
                }
            }
        }
    }

    /// Mark a token exchange as completed in Redis.
    pub async fn mark_exchange_completed(
        redis: &mut redis::aio::ConnectionManager,
        key: &str,
        result: &serde_json::Value,
    ) {
        let value = serde_json::json!({
            "status": "completed",
            "result": result,
        });
        let _: () = redis::cmd("SET")
            .arg(key)
            .arg(value.to_string())
            .arg("EX")
            .arg(300) // 5 minute TTL
            .query_async(redis)
            .await
            .unwrap_or(());
    }

    /// Clean up idempotency state on failure.
    pub async fn cleanup_idempotency(redis: &mut redis::aio::ConnectionManager, key: &str) {
        let _: () = redis::cmd("DEL")
            .arg(key)
            .query_async(redis)
            .await
            .unwrap_or(());
    }

    // -----------------------------------------------------------------------
    // Refresh + reissue MCP JWT (for grant_type=refresh_token)
    // -----------------------------------------------------------------------

    /// Verify an MCP JWT allowing expired tokens (for refresh flows).
    /// The client's MCP JWT will be expired — that's why they're refreshing.
    pub fn verify_mcp_jwt_allow_expired(&self, token: &str) -> Result<McpJwtClaims, OAuthError> {
        let algo = match self.jwt_algorithm.as_str() {
            "HS384" => jsonwebtoken::Algorithm::HS384,
            "HS512" => jsonwebtoken::Algorithm::HS512,
            _ => jsonwebtoken::Algorithm::HS256,
        };
        let mut validation = Validation::new(algo);
        validation.set_issuer(&[JWT_ISSUER]);
        validation.validate_exp = false; // Allow expired tokens for refresh

        let data = decode::<McpJwtClaims>(
            token,
            &DecodingKey::from_secret(self.jwt_secret.as_bytes()),
            &validation,
        )
        .map_err(|e| OAuthError::JwtError(e.to_string()))?;

        if data.claims.token_type != "mcp_auth" {
            return Err(OAuthError::JwtError("Invalid token type".into()));
        }

        Ok(data.claims)
    }

    /// Refresh the stored OAuth token and reissue a new MCP JWT.
    /// Called when a client sends grant_type=refresh_token with their MCP JWT.
    pub async fn refresh_and_reissue_jwt(
        &self,
        db: &PgPool,
        claims: &McpJwtClaims,
    ) -> Result<(String, bool), OAuthError> {
        let tenant_id = &claims.tenant_id;
        let idp_user_id = &claims.user_id;
        let mcp_server_id = &claims.mcp_server_id;
        let idp_id = &claims.idp_id;

        // Look up stored token
        let token: OAuthToken = sqlx::query_as(
            r#"SELECT * FROM oauth_tokens
               WHERE tenant_id = $1 AND idp_user_id = $2 AND mcp_server_id = $3
                 AND idp_id = $4 AND revoked = false
               ORDER BY created_at DESC LIMIT 1"#,
        )
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .fetch_optional(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?
        .ok_or_else(|| {
            OAuthError::RefreshFailed("No stored OAuth token found for this user/server".into())
        })?;

        if token.revoked {
            return Err(OAuthError::TokenRevoked);
        }

        // If the stored OAuth token is expired, refresh it with IdP
        let has_refresh = token.encrypted_refresh_token.is_some();
        if token.is_expired() {
            if !has_refresh {
                return Err(OAuthError::RefreshFailed(
                    "OAuth token expired and no refresh token available — user must re-authenticate".into(),
                ));
            }

            // Refresh with per-token lock
            let lock_key = format!("{tenant_id}:{idp_id}:{idp_user_id}:{mcp_server_id}");
            let lock = {
                let mut locks = self.refresh_locks.lock().await;
                locks
                    .entry(lock_key.clone())
                    .or_insert_with(|| Arc::new(Mutex::new(())))
                    .clone()
            };

            let _guard = lock.lock().await;

            // Re-check after lock
            let token: OAuthToken = sqlx::query_as(
                r#"SELECT * FROM oauth_tokens
                   WHERE tenant_id = $1 AND idp_user_id = $2 AND mcp_server_id = $3
                     AND idp_id = $4 AND revoked = false
                   ORDER BY created_at DESC LIMIT 1"#,
            )
            .bind(tenant_id)
            .bind(idp_user_id)
            .bind(mcp_server_id)
            .bind(idp_id)
            .fetch_optional(db)
            .await
            .map_err(|e| OAuthError::DbError(e.to_string()))?
            .ok_or_else(|| OAuthError::RefreshFailed("Token disappeared during refresh".into()))?;

            if token.is_expired() {
                self.refresh_stored_token(db, &token, None, tenant_id)
                    .await?;
            }

            // Clean up lock
            {
                let mut locks = self.refresh_locks.lock().await;
                locks.remove(&lock_key);
            }
        }

        // Mint new MCP JWT
        let mcp_jwt = self.create_mcp_jwt(
            tenant_id,
            idp_user_id,
            mcp_server_id,
            idp_id,
            claims.username.as_deref(),
            claims.email.as_deref(),
            claims.name.as_deref(),
        )?;

        Ok((mcp_jwt, has_refresh))
    }

    // -----------------------------------------------------------------------
    // Token revocation
    // -----------------------------------------------------------------------

    /// Revoke an OAuth token (marks as revoked in DB).
    pub async fn revoke_token(
        &self,
        db: &PgPool,
        idp_user_id: &str,
        mcp_server_id: &str,
        tenant_id: &str,
        idp_id: &str,
    ) -> Result<bool, OAuthError> {
        let result = sqlx::query(
            r#"UPDATE oauth_tokens
               SET revoked = true, revoked_at = NOW(), updated_at = NOW()
               WHERE tenant_id = $1 AND idp_user_id = $2 AND mcp_server_id = $3
                 AND idp_id = $4 AND revoked = false"#,
        )
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(mcp_server_id)
        .bind(idp_id)
        .execute(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?;

        Ok(result.rows_affected() > 0)
    }

    // -----------------------------------------------------------------------
    // Token listing
    // -----------------------------------------------------------------------

    /// Get all tokens for a user (metadata only, no secrets). `idp_id`, when
    /// the caller is an IdP subject (an MCP JWT), is that subject's IdP: the
    /// rows of another IdP under the same subject string are not theirs.
    pub async fn get_all_tokens_for_user(
        &self,
        db: &PgPool,
        idp_user_id: &str,
        tenant_id: &str,
        idp_id: Option<&str>,
    ) -> Result<Vec<TokenSummary>, OAuthError> {
        let tokens: Vec<OAuthToken> = sqlx::query_as(
            r#"SELECT * FROM oauth_tokens
               WHERE tenant_id = $1 AND idp_user_id = $2
                 AND ($3::text IS NULL OR idp_id = $3)
               ORDER BY created_at DESC"#,
        )
        .bind(tenant_id)
        .bind(idp_user_id)
        .bind(idp_id)
        .fetch_all(db)
        .await
        .map_err(|e| OAuthError::DbError(e.to_string()))?;

        let now = Utc::now().naive_utc();
        Ok(tokens
            .into_iter()
            .map(|t| {
                let is_expired = t.expires_at < now;
                TokenSummary {
                    mcp_server_id: t.mcp_server_id,
                    tenant_id: t.tenant_id,
                    idp_id: t.idp_id,
                    token_type: t.token_type,
                    expires_at: t.expires_at.and_utc().to_rfc3339(),
                    is_expired,
                    scopes: t
                        .scopes
                        .map(|s| s.split_whitespace().map(String::from).collect()),
                    has_refresh_token: t.encrypted_refresh_token.is_some(),
                    created_at: t.created_at.and_utc().to_rfc3339(),
                    last_refreshed_at: t.last_refreshed_at.map(|d| d.and_utc().to_rfc3339()),
                    refresh_count: t.refresh_count,
                    revoked: t.revoked,
                }
            })
            .collect())
    }

    // -----------------------------------------------------------------------
    // Cleanup & statistics
    // -----------------------------------------------------------------------

    /// Delete expired tokens older than N days.
    pub async fn cleanup_expired_tokens(
        &self,
        db: &PgPool,
        tenant_id: Option<&str>,
        older_than_days: i64,
    ) -> Result<u64, OAuthError> {
        let cutoff = (Utc::now() - Duration::days(older_than_days)).naive_utc();

        let result = match tenant_id {
            Some(tid) => {
                sqlx::query("DELETE FROM oauth_tokens WHERE expires_at < $1 AND tenant_id = $2")
                    .bind(cutoff)
                    .bind(tid)
                    .execute(db)
                    .await
            }
            None => {
                sqlx::query("DELETE FROM oauth_tokens WHERE expires_at < $1")
                    .bind(cutoff)
                    .execute(db)
                    .await
            }
        }
        .map_err(|e| OAuthError::DbError(e.to_string()))?;

        Ok(result.rows_affected())
    }
}

/// Tokens extracted from an IdP token response.
#[derive(Debug, Clone)]
pub struct ExtractedTokens {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_in: i64,
    pub token_type: String,
    pub scope: Option<String>,
}

/// Token metadata summary (no secrets).
#[derive(Debug, Clone, Serialize)]
pub struct TokenSummary {
    pub mcp_server_id: String,
    pub tenant_id: String,
    pub idp_id: String,
    pub token_type: Option<String>,
    pub expires_at: String,
    pub is_expired: bool,
    pub scopes: Option<Vec<String>>,
    pub has_refresh_token: bool,
    pub created_at: String,
    pub last_refreshed_at: Option<String>,
    pub refresh_count: i32,
    pub revoked: bool,
}

/// Token usage statistics.
#[derive(Debug, Clone, Serialize)]
pub struct TokenStatistics {
    pub tenant_id: String,
    pub total_tokens: i64,
    pub active_tokens: i64,
    pub expired_tokens: i64,
    pub revoked_tokens: i64,
    pub tokens_with_refresh: i64,
}

/// Classify an `OAuthError` into a short, stable error_code suitable for
/// use as the dedup key on a downstream IdP alert. We only ever call this
/// on errors that already passed the is-this-downstream filter, so every
/// variant has a meaningful translation.
fn classify_oauth_error(err: &OAuthError) -> String {
    match err {
        OAuthError::ConfigError(_) => "config_error".into(),
        OAuthError::ExchangeFailed(_) => "exchange_failed".into(),
        OAuthError::RefreshFailed(_) => "refresh_failed".into(),
        OAuthError::TokenRevoked => "token_revoked".into(),
        OAuthError::EncryptionError(_) => "encryption_error".into(),
        OAuthError::JwtError(_) => "jwt_error".into(),
        OAuthError::UserInfoFailed(_) => "userinfo_failed".into(),
        OAuthError::DbError(_) => "db_error".into(),
        OAuthError::DuplicateExchange => "duplicate_exchange".into(),
    }
}

/// Refuse an IdP whose server-side endpoints (token, userinfo,
/// registration, JWKS, revocation, introspection) include one the outbound
/// policy denies. They are tenant-configured; the policy resolver covers
/// hostnames, but an IP literal (`http://169.254.169.254/…`) would be
/// fetched directly. The browser-facing authorization endpoint is not
/// fetched by the proxy and is not checked.
pub fn check_idp_destinations(
    idp: &IdpServer,
) -> Result<(), crate::services::outbound_policy::Denied> {
    for url in [
        &idp.token_endpoint,
        &idp.userinfo_endpoint,
        &idp.registration_endpoint,
        &idp.jwks_uri,
        &idp.revocation_endpoint,
        &idp.introspection_endpoint,
    ]
    .into_iter()
    .flatten()
    {
        let url = url.trim();
        if url.is_empty() {
            continue;
        }
        crate::services::outbound_policy::check_url(url).inspect_err(|denied| {
            tracing::warn!(
                idp_id = %idp.id,
                host = %denied.host,
                reason = denied.reason,
                "IdP endpoint refused by outbound policy"
            );
        })?;
    }
    Ok(())
}

#[derive(Debug, thiserror::Error)]
pub enum OAuthError {
    #[error("Configuration error: {0}")]
    ConfigError(String),
    #[error("Token exchange failed: {0}")]
    ExchangeFailed(String),
    #[error("Token refresh failed: {0}")]
    RefreshFailed(String),
    #[error("Token has been revoked")]
    TokenRevoked,
    #[error("Encryption error: {0}")]
    EncryptionError(String),
    #[error("JWT error: {0}")]
    JwtError(String),
    #[error("User info retrieval failed: {0}")]
    UserInfoFailed(String),
    #[error("Database error: {0}")]
    DbError(String),
    #[error("Duplicate token exchange in progress")]
    DuplicateExchange,
}

impl OAuthError {
    pub fn status_code(&self) -> axum::http::StatusCode {
        use axum::http::StatusCode;
        match self {
            OAuthError::ConfigError(_) => StatusCode::INTERNAL_SERVER_ERROR,
            OAuthError::ExchangeFailed(_) => StatusCode::BAD_GATEWAY,
            OAuthError::RefreshFailed(_) => StatusCode::BAD_GATEWAY,
            OAuthError::TokenRevoked => StatusCode::UNAUTHORIZED,
            OAuthError::EncryptionError(_) => StatusCode::INTERNAL_SERVER_ERROR,
            OAuthError::JwtError(_) => StatusCode::INTERNAL_SERVER_ERROR,
            OAuthError::UserInfoFailed(_) => StatusCode::UNAUTHORIZED,
            OAuthError::DbError(_) => StatusCode::INTERNAL_SERVER_ERROR,
            OAuthError::DuplicateExchange => StatusCode::CONFLICT,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn idp_with(token_endpoint: Option<&str>, jwks_uri: Option<&str>) -> IdpServer {
        IdpServer {
            id: "idp-d32".into(),
            tenant_id: "t".into(),
            name: "idp".into(),
            issuer: "https://idp.example".into(),
            idp_type: None,
            well_known_url: None,
            authorization_endpoint: Some("http://169.254.169.254/authorize".into()),
            token_endpoint: token_endpoint.map(Into::into),
            revocation_endpoint: None,
            introspection_endpoint: None,
            jwks_uri: jwks_uri.map(Into::into),
            userinfo_endpoint: None,
            registration_endpoint: None,
            supports_pkce: None,
            supports_refresh_tokens: None,
            supports_client_credentials: None,
            token_endpoint_auth_methods: None,
            scopes_supported: None,
            response_types_supported: None,
            grant_types_supported: None,
            cached_config: None,
            last_discovered: None,
            discovery_cache_ttl: None,
            enabled: true,
            verified: None,
            description: None,
            logo_url: None,
            created_at: None,
            updated_at: None,
        }
    }

    /// An IdP whose server-side endpoint is a denied IP literal is
    /// refused; the browser-facing authorization endpoint is not fetched by
    /// the proxy and is not judged.
    #[test]
    fn idp_endpoints_the_policy_denies_are_refused() {
        assert!(
            check_idp_destinations(&idp_with(Some("http://169.254.169.254/token"), None)).is_err()
        );
        assert!(
            check_idp_destinations(&idp_with(None, Some("http://[fd00:ec2::254]/jwks"))).is_err()
        );
        assert!(
            check_idp_destinations(&idp_with(Some("https://idp.example/token"), None)).is_ok(),
            "a public token endpoint passes even though authorization_endpoint is a literal"
        );
    }

    #[test]
    fn classify_oauth_error_covers_every_variant() {
        // Pinning the mapping here means a downstream system alert
        // query keyed on these codes stays stable across refactors —
        // and adding a new OAuthError variant forces this test to
        // refuse to compile (match exhaustiveness), which is the point.
        let cases: &[(OAuthError, &str)] = &[
            (OAuthError::ConfigError("x".into()), "config_error"),
            (OAuthError::ExchangeFailed("x".into()), "exchange_failed"),
            (OAuthError::RefreshFailed("x".into()), "refresh_failed"),
            (OAuthError::TokenRevoked, "token_revoked"),
            (OAuthError::EncryptionError("x".into()), "encryption_error"),
            (OAuthError::JwtError("x".into()), "jwt_error"),
            (OAuthError::UserInfoFailed("x".into()), "userinfo_failed"),
            (OAuthError::DbError("x".into()), "db_error"),
            (OAuthError::DuplicateExchange, "duplicate_exchange"),
        ];
        for (err, expected) in cases {
            assert_eq!(
                classify_oauth_error(err),
                *expected,
                "variant {err:?} mapped to the wrong code"
            );
        }
    }

    #[test]
    fn classify_oauth_error_codes_are_all_snake_case() {
        // The code is used as part of the system_alerts dedup_key;
        // mixing casing would split otherwise-identical failures
        // into two alert rows. Keep them lowercase/snake_case.
        let sample_errors = [
            OAuthError::ConfigError("x".into()),
            OAuthError::ExchangeFailed("x".into()),
            OAuthError::RefreshFailed("x".into()),
            OAuthError::TokenRevoked,
            OAuthError::EncryptionError("x".into()),
            OAuthError::JwtError("x".into()),
            OAuthError::UserInfoFailed("x".into()),
            OAuthError::DbError("x".into()),
            OAuthError::DuplicateExchange,
        ];
        for err in &sample_errors {
            let code = classify_oauth_error(err);
            assert!(!code.is_empty());
            assert_eq!(code, code.to_lowercase());
            assert!(!code.contains(' '));
        }
    }
}
