// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The evidence signing key and issuer.
//!
//! Separate from `BRUTOR_AUDIT_SIGNING_KEY` on purpose: each key is bound
//! to a closed set of shapes (the audit key to row-chain checkpoints and
//! manifests; this one to record statements and tree heads), so neither is
//! a signing oracle for the other's artefacts. Loaded once, like the audit
//! key; an absent key means no records are minted and the console says so
//! — an evidence-quality setting must never become an outage.
//!
//! # Per-tenant keys
//!
//! The deployment key (`BRUTOR_EVIDENCE_SIGNING_KEY`) is the default and the
//! master switch: without it nothing is sealed. A tenant opts into its own
//! key with `evidence_settings.per_tenant_key`; the key itself is created by
//! the first rotation (`POST /v1/evidence/keys/{tenant}/rotate`) and stored
//! in `evidence_signing_keys` as a Fernet-encrypted seed under
//! `ENCRYPTION_KEY` — the same envelope the control plane uses for provider
//! secrets. [`signing_key_for`] resolves the key a tenant's records and
//! heads are signed with: the active tenant key when the setting is on and
//! a key exists, the deployment key otherwise. Resolution is cached for
//! [`TENANT_KEY_TTL_SECS`] and invalidated in-process on rotation, so
//! another replica may sign with the retired key for up to that long —
//! harmless, because retired kids stay trusted for verification forever.
//!
//! A tenant that has its own active key is never sealed under another one.
//! When that key cannot be used — the lookup
//! failed, the stored seed does not decrypt (another `ENCRYPTION_KEY`, a
//! damaged row) or does not match its kid — resolution is an error
//! ([`TenantKeyError`]), nothing is sealed for the tenant, and the answer is
//! not cached. This used to fall back to the deployment key with a warning:
//! the tenant's records and tree heads went on being signed, by a key the
//! tenant's verifier does not pin. No request fails for it: the writer's
//! rows land without a record and `POST /v1/evidence/backfill` seals them
//! once the key is usable; [`rotate`] is the way out when it never will be.

use crate::services::encryption::EncryptionService;
use ed25519_dalek::SigningKey;
use moka::future::Cache;
use sqlx::PgPool;
use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use crate::env_registry::{Env, EnvError};
use crate::env_vars;

pub const TENANT_KEY_TTL_SECS: u64 = 60;

#[cfg(feature = "db-tests")]
tokio::task_local! {
    /// Integration-test hook: inside `FORCE_DISABLED.scope(true, …)` the
    /// deployment key reads as absent for this task only, so a test can
    /// exercise the "evidence disabled" paths without mutating the
    /// process-wide environment other tests depend on.
    pub static FORCE_DISABLED: bool;
}

/// The deployment evidence key `env` configures
/// (`BRUTOR_EVIDENCE_SIGNING_KEY`); `None` when it is unset. A value that is
/// not a 32-byte seed in hexadecimal is refused: the gateway does not start
/// with it (`config::check`). It used to read as "no key", and nothing was
/// sealed.
pub fn signing_key_from(env: &Env) -> Result<Option<SigningKey>, EnvError> {
    let var = &env_vars::BRUTOR_EVIDENCE_SIGNING_KEY;
    let Some(raw) = env.string(var) else {
        return Ok(None);
    };
    env_vars::hex_32(&raw)
        .map(|seed| Some(SigningKey::from_bytes(&seed)))
        .ok_or_else(|| env_vars::invalid(var, var.accepted))
}

/// The evidence key, derived again when the configured value changes.
///
/// Unlike the audit key this is not a `OnceLock`: the writer asks for it
/// once per batch (not per row), and a cache that could only ever be filled
/// once made the integration tests order-dependent — whichever test flushed
/// first fixed the answer for the whole process.
pub fn signing_key() -> Option<Arc<SigningKey>> {
    #[cfg(feature = "db-tests")]
    if FORCE_DISABLED.try_with(|v| *v).unwrap_or(false) {
        return None;
    }
    static CACHE: OnceLock<RwLock<Option<(String, Option<Arc<SigningKey>>)>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| RwLock::new(None));
    let raw = env_vars::process()
        .string(&env_vars::BRUTOR_EVIDENCE_SIGNING_KEY)
        .unwrap_or_default();
    if let Ok(guard) = cache.read()
        && let Some((cached_raw, key)) = guard.as_ref()
        && *cached_raw == raw
    {
        return key.clone();
    }
    let parsed = env_vars::hex_32(&raw).map(|seed| Arc::new(SigningKey::from_bytes(&seed)));
    if let Ok(mut guard) = cache.write() {
        *guard = Some((raw, parsed.clone()));
    }
    parsed
}

pub fn is_enabled() -> bool {
    signing_key().is_some()
}

/// Lowercase hex of the raw 32-byte public key — the `kid` on every
/// statement and the value published at `/.well-known/brutor-evidence-keys.json`.
pub fn kid_hex() -> Option<String> {
    signing_key().map(|k| hex::encode(k.verifying_key().to_bytes()))
}

/// The deployment's identity as `env` configures it
/// (`BRUTOR_DEPLOYMENT_ID`, `default` unless set).
pub fn deployment_id_from(env: &Env) -> String {
    env.string(&env_vars::BRUTOR_DEPLOYMENT_ID)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "default".to_string())
}

pub fn deployment_id() -> String {
    deployment_id_from(&env_vars::process())
}

/// CWT `iss` of every statement this deployment seals, as `env` configures
/// it (`BRUTOR_EVIDENCE_ISSUER`): a URN derived from the deployment id
/// unless set; a deployment that publishes a `did:web` sets it.
pub fn issuer_from(env: &Env) -> String {
    env.string(&env_vars::BRUTOR_EVIDENCE_ISSUER)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| format!("urn:brutor:deployment:{}", deployment_id_from(env)))
}

pub fn issuer() -> String {
    issuer_from(&env_vars::process())
}

/// `log_id` a NEW tenant log is created with: SHA-256 over
/// `deployment ‖ 0 ‖ tenant`, never a name or a path.
///
/// Only for creation. Once a log exists its id is fixed — read it with
/// [`log_id_of`]. Recomputing it on every write made a changed
/// `BRUTOR_DEPLOYMENT_ID` (e.g. a fresh trial download over an old DB
/// volume) fail every seal: `evidence_logs.tenant_id` is unique, so the
/// "new" log could never be created and the old one was never found.
pub fn log_id_for(tenant_id: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(deployment_id().as_bytes());
    h.update([0u8]);
    h.update(tenant_id.as_bytes());
    hex::encode(h.finalize())
}

/// The tenant's log id: the stored one when the log exists, else the id it
/// will be created with ([`log_id_for`]).
pub async fn log_id_of<'e, E>(executor: E, tenant_id: &str) -> Result<String, sqlx::Error>
where
    E: sqlx::PgExecutor<'e>,
{
    let stored: Option<String> =
        sqlx::query_scalar("SELECT log_id FROM evidence_logs WHERE tenant_id = $1")
            .bind(tenant_id)
            .fetch_optional(executor)
            .await?;
    Ok(stored.unwrap_or_else(|| log_id_for(tenant_id)))
}

/// Boot-time check: logs whose stored id no longer matches the configured
/// deployment id. Not an error — each log keeps its identity — but the
/// deployment id is part of how new logs are named and how an operator
/// reads the well-known document, so a silent change deserves a warning.
pub async fn warn_on_deployment_drift(pool: &PgPool) {
    let logs: Vec<(String, String)> =
        match sqlx::query_as("SELECT log_id, tenant_id FROM evidence_logs")
            .fetch_all(pool)
            .await
        {
            Ok(l) => l,
            Err(_) => return, // pre-a177 schema: nothing to check
        };
    let drifted = logs
        .iter()
        .filter(|(log_id, tenant)| *log_id != log_id_for(tenant))
        .count();
    if drifted > 0 {
        tracing::warn!(
            target = "brutor::evidence",
            drifted,
            total = logs.len(),
            deployment_id = %deployment_id(),
            "evidence logs were created under a different BRUTOR_DEPLOYMENT_ID; they keep their \
             original log ids and continue to append. Restore the original value if this was \
             unintended (e.g. a new trial download over an existing database volume).",
        );
    }
}

fn jwk(raw: &[u8; 32], status: &str, scope: &str, log_id: Option<&str>) -> serde_json::Value {
    use base64::Engine as _;
    let mut v = serde_json::json!({
        "kid": hex::encode(raw),
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "x": base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw),
        "use": "sig",
        "issuer": issuer(),
        "status": status,
        "scope": scope,
    });
    if let Some(l) = log_id {
        v["log_id"] = serde_json::json!(l);
    }
    v
}

/// The retired deployment public keys `env` configures
/// (`BRUTOR_EVIDENCE_RETIRED_KEYS`: comma-separated raw Ed25519 public keys,
/// hex), in lower case. Verification keeps trusting them; nothing is signed
/// with them. An entry that is not 64 hexadecimal characters is refused: the
/// gateway does not start with it (`config::check`). It used to be dropped,
/// and what that key signed stopped verifying without a word.
pub fn retired_kids_from(env: &Env) -> Result<Vec<String>, EnvError> {
    let var = &env_vars::BRUTOR_EVIDENCE_RETIRED_KEYS;
    env.list(var)
        .into_iter()
        .map(|kid| {
            let kid = kid.to_ascii_lowercase();
            env_vars::hex_32(&kid)
                .map(|_| kid)
                .ok_or_else(|| env_vars::invalid(var, var.accepted))
        })
        .collect()
}

/// Retired deployment public keys (`BRUTOR_EVIDENCE_RETIRED_KEYS`).
pub fn retired_deployment_kids() -> Vec<String> {
    retired_kids_from(&env_vars::process()).unwrap_or_default()
}

/// Public-key document served at the well-known path: the deployment key
/// only (no database). See [`well_known_document_with_tenants`].
pub fn well_known_document() -> serde_json::Value {
    document(Vec::new())
}

fn document(extra: Vec<serde_json::Value>) -> serde_json::Value {
    let mut keys: Vec<serde_json::Value> = signing_key()
        .map(|k| {
            vec![jwk(
                &k.verifying_key().to_bytes(),
                "active",
                "deployment",
                None,
            )]
        })
        .unwrap_or_default();
    for kid in retired_deployment_kids() {
        if let Some(raw) = hex::decode(&kid)
            .ok()
            .and_then(|b| <[u8; 32]>::try_from(b).ok())
            && !keys.iter().any(|k| k["kid"] == kid.as_str())
        {
            keys.push(jwk(&raw, "retired", "deployment", None));
        }
    }
    for e in extra {
        if !keys.iter().any(|k| k["kid"] == e["kid"]) {
            keys.push(e);
        }
    }
    serde_json::json!({
        "issuer": issuer(),
        "keys": keys,
        "statement_content_type": brutor_evidence::statement::RECORD_CONTENT_TYPE,
        "tree_head_content_type": brutor_ctlog::TREE_HEAD_CONTENT_TYPE,
        "record_schema": brutor_evidence::SCHEMA,
    })
}

/// The well-known document with every stored key and its status. Tenant
/// keys are listed by `log_id` (an opaque hash), never by tenant id — the
/// document is public.
///
/// A database error is returned: a document missing the tenant keys
/// is published, cached for five minutes, and makes their records fail
/// verification.
pub async fn well_known_document_with_tenants(
    pool: &PgPool,
) -> Result<serde_json::Value, sqlx::Error> {
    let rows = stored_keys(pool).await?;
    let log_ids: std::collections::HashMap<String, String> =
        sqlx::query_as::<_, (String, String)>("SELECT tenant_id, log_id FROM evidence_logs")
            .fetch_all(pool)
            .await?
            .into_iter()
            .collect();
    let extra = rows
        .iter()
        .filter_map(|r| {
            let raw = hex::decode(&r.kid)
                .ok()
                .and_then(|b| <[u8; 32]>::try_from(b).ok())?;
            let log = r
                .tenant_id
                .as_deref()
                .map(|t| log_ids.get(t).cloned().unwrap_or_else(|| log_id_for(t)));
            Some(jwk(
                &raw,
                &r.status,
                if r.tenant_id.is_some() {
                    "tenant"
                } else {
                    "deployment"
                },
                log.as_deref(),
            ))
        })
        .collect();
    Ok(document(extra))
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct StoredKey {
    pub kid: String,
    pub tenant_id: Option<String>,
    pub status: String,
}

/// Every row of `evidence_signing_keys`. A database error is returned:
/// read as "no stored keys", every tenant-signed record would fail
/// verification as signed by an untrusted key.
pub async fn stored_keys(pool: &PgPool) -> Result<Vec<StoredKey>, sqlx::Error> {
    sqlx::query_as::<_, StoredKey>(
        "SELECT kid, tenant_id, status FROM evidence_signing_keys ORDER BY created_at",
    )
    .fetch_all(pool)
    .await
}

/// Every kid this deployment will accept a signature under: the current
/// deployment key, retired deployment keys, and every stored key (active
/// or retired). A retired kid is still trusted — retiring stops signing,
/// never verification.
pub async fn trusted_kids(pool: &PgPool) -> Result<std::collections::HashSet<String>, sqlx::Error> {
    let mut out: std::collections::HashSet<String> =
        retired_deployment_kids().into_iter().collect();
    if let Some(k) = kid_hex() {
        out.insert(k);
    }
    for r in stored_keys(pool).await? {
        out.insert(r.kid.to_ascii_lowercase());
    }
    Ok(out)
}

/// Status of every kid, for the verify responses (`active|retired`).
pub async fn kid_statuses(pool: &PgPool) -> Result<BTreeMap<String, String>, sqlx::Error> {
    let mut out = BTreeMap::new();
    for k in retired_deployment_kids() {
        out.insert(k, "retired".to_string());
    }
    for r in stored_keys(pool).await? {
        out.insert(r.kid.to_ascii_lowercase(), r.status);
    }
    if let Some(k) = kid_hex() {
        out.insert(k, "active".to_string());
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Per-tenant keys
// ---------------------------------------------------------------------------

fn encryption_slot() -> &'static OnceLock<Option<EncryptionService>> {
    static ENC: OnceLock<Option<EncryptionService>> = OnceLock::new();
    &ENC
}

/// Install the process encryption service (from `AppState`). Called at
/// startup; idempotent. Without it the service is derived once from
/// `ENCRYPTION_KEY`.
pub fn install_encryption(enc: EncryptionService) {
    let _ = encryption_slot().set(Some(enc));
}

fn encryption() -> Option<&'static EncryptionService> {
    encryption_slot()
        .get_or_init(|| {
            env_vars::process()
                .string(&env_vars::ENCRYPTION_KEY)
                .filter(|k| !k.trim().is_empty())
                .map(|k| EncryptionService::new(&k))
        })
        .as_ref()
}

type TenantKeyCacheKey = (String, String);

fn tenant_key_cache() -> &'static Cache<TenantKeyCacheKey, Option<Arc<SigningKey>>> {
    static C: OnceLock<Cache<TenantKeyCacheKey, Option<Arc<SigningKey>>>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(TENANT_KEY_TTL_SECS))
            .build()
    })
}

fn cache_key(pool: &PgPool, tenant: &str) -> TenantKeyCacheKey {
    (
        pool.connect_options()
            .get_database()
            .unwrap_or("")
            .to_string(),
        tenant.to_string(),
    )
}

/// Drop the cached resolution for `tenant` (after a rotation).
pub async fn invalidate_tenant(pool: &PgPool, tenant: &str) {
    tenant_key_cache()
        .invalidate(&cache_key(pool, tenant))
        .await;
}

/// Seal a seed for storage.
pub fn encrypt_seed(seed: &[u8; 32]) -> Result<String, String> {
    let enc = encryption().ok_or("ENCRYPTION_KEY is not configured")?;
    enc.encrypt(&hex::encode(seed)).map_err(|e| e.to_string())
}

fn decrypt_seed(ct: &str) -> Option<SigningKey> {
    let plain = encryption()?.decrypt(ct).ok()?;
    let seed: [u8; 32] = hex::decode(plain.trim()).ok()?.try_into().ok()?;
    Some(SigningKey::from_bytes(&seed))
}

/// Why a tenant's own key could not be resolved. Never carries the seed.
#[derive(Debug, thiserror::Error)]
pub enum TenantKeyError {
    #[error("the tenant's evidence signing key could not be looked up: {0}")]
    Lookup(#[from] sqlx::Error),
    #[error(
        "the tenant's evidence signing key {kid} cannot be decrypted or does not match its kid; \
         nothing is sealed for this tenant until it is rotated or ENCRYPTION_KEY is restored"
    )]
    Unusable { kid: String },
}

/// The tenant's own active key, when `per_tenant_key` is on and one exists.
/// `Ok(None)` = the tenant has no key of its own: use the deployment key.
/// `Err` = it has one and it cannot be used, or the lookup failed.
pub async fn load_tenant_key(
    pool: &PgPool,
    tenant: &str,
) -> Result<Option<Arc<SigningKey>>, TenantKeyError> {
    let row: Option<(bool, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT s.per_tenant_key, k.kid, k.encrypted_seed FROM evidence_settings s          LEFT JOIN evidence_signing_keys k ON k.tenant_id = s.tenant_id AND k.status = 'active'          WHERE s.tenant_id = $1",
    )
    .bind(tenant)
    .fetch_optional(pool)
    .await?;
    let Some((true, Some(kid), Some(ct))) = row else {
        return Ok(None);
    };
    match decrypt_seed(&ct) {
        Some(k) if hex::encode(k.verifying_key().to_bytes()) == kid.to_ascii_lowercase() => {
            Ok(Some(Arc::new(k)))
        }
        _ => Err(TenantKeyError::Unusable { kid }),
    }
}

/// Whether the tenant opted into its own key (`evidence_settings`).
pub async fn per_tenant_key_enabled(pool: &PgPool, tenant: &str) -> Result<bool, sqlx::Error> {
    let row: Option<(bool,)> =
        sqlx::query_as("SELECT per_tenant_key FROM evidence_settings WHERE tenant_id = $1")
            .bind(tenant)
            .fetch_optional(pool)
            .await?;
    Ok(row.map(|(b,)| b).unwrap_or(false))
}

/// The key `tenant`'s records and heads are signed with. `Ok(None)` only
/// when evidence is disabled (no deployment key).
///
/// `Err` when the tenant has its own key and it cannot be used:
/// the caller seals nothing. An error is not cached — the next call
/// asks again.
pub async fn signing_key_for(
    pool: &PgPool,
    tenant: &str,
) -> Result<Option<Arc<SigningKey>>, TenantKeyError> {
    let Some(deployment) = signing_key() else {
        return Ok(None);
    };
    let ck = cache_key(pool, tenant);
    if let Some(hit) = tenant_key_cache().get(&ck).await {
        return Ok(Some(hit.unwrap_or(deployment)));
    }
    let resolved = load_tenant_key(pool, tenant).await.inspect_err(|e| {
        tracing::error!(
            target = "brutor::evidence",
            tenant_id = %tenant,
            error = %e,
            "per-tenant evidence key unavailable; nothing is sealed for this tenant \
             (rows land without a record; backfill seals them once the key is usable)",
        );
    })?;
    tenant_key_cache().insert(ck, resolved.clone()).await;
    Ok(Some(resolved.unwrap_or(deployment)))
}

/// Lowercase hex of a key's raw public half.
pub fn kid_of(key: &SigningKey) -> String {
    hex::encode(key.verifying_key().to_bytes())
}

// ---------------------------------------------------------------------------
// Rotation
// ---------------------------------------------------------------------------

pub const ROTATION_RECORD_TYPE: &str = "ai.brutor.key-rotation";

#[derive(Debug, thiserror::Error)]
pub enum RotateError {
    #[error(
        "evidence signing key is not configured ({})",
        env_vars::BRUTOR_EVIDENCE_SIGNING_KEY.name
    )]
    Disabled,
    #[error(
        "per-tenant evidence keys are not enabled for this tenant (evidence_settings.per_tenant_key)"
    )]
    NotEnabled,
    #[error("{0}")]
    Encryption(String),
    #[error("{0}")]
    Record(String),
    #[error(transparent)]
    Seal(#[from] crate::services::evidence::store::SealError),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    #[error(transparent)]
    TenantKey(TenantKeyError),
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Rotation {
    pub tenant_id: String,
    pub kid: String,
    pub previous_kid: String,
    pub rotation_record_id: String,
    pub log_id: String,
    pub seq: i64,
}

/// The `ai.brutor.key-rotation` record: `fyi`, both kids in clear (public
/// keys are clear-safe), and a SHA-256 reference to the new public key.
pub fn rotation_record(
    tenant: &str,
    old_kid: &str,
    new_key: &ed25519_dalek::VerifyingKey,
    at: chrono::DateTime<chrono::Utc>,
) -> Result<crate::services::evidence::mint::Minted, brutor_evidence::RecordError> {
    use brutor_evidence::record::{
        Disposition, RecordBuilder, Reference, approver, decision, kind, verdict,
    };
    use sha2::{Digest, Sha256};
    let new_kid = hex::encode(new_key.to_bytes());
    let mut b = RecordBuilder::new(
        format!("{ROTATION_RECORD_TYPE}/{new_kid}"),
        tenant,
        at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    );
    b.kind = kind::FYI.into();
    b.verdict = verdict::RECORDED.into();
    b.disposition = Some(Disposition {
        decision: decision::ACCEPT.into(),
        approver: approver::POLICY.into(),
        human_disposed: false,
        authority: None,
        reason_digest: None,
    });
    let mut ctx = serde_json::Map::new();
    ctx.insert(
        "record_type".into(),
        serde_json::json!(ROTATION_RECORD_TYPE),
    );
    ctx.insert("old_kid".into(), serde_json::json!(old_kid));
    ctx.insert("new_kid".into(), serde_json::json!(new_kid));
    b.context = ctx;
    b.references = Some(vec![Reference {
        kind: "ai.brutor.evidence-key".into(),
        digest_alg: "SHA-256".into(),
        digest: hex::encode(Sha256::digest(new_key.to_bytes())),
        purpose: Some("new_key".into()),
    }]);
    let (record, record_id) = b.seal()?;
    Ok(crate::services::evidence::mint::Minted {
        row_index: 0,
        tenant_id: tenant.to_string(),
        record,
        record_id,
        ai_system_id: None,
        root_task_id: None,
        proxy_log_id: None,
        verdict: verdict::RECORDED.into(),
        kind: kind::FYI.into(),
        surface: "evidence".into(),
        epoch: None,
        timestamp: at,
        tags: None,
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

/// Rotate `tenant`'s key (creating its first one if it has none): seal the
/// rotation record with the key in force *now* (the old key — so the
/// hand-over is signed by the key a relying party already trusts), then
/// retire the old tenant key and store the new one, in one transaction.
pub async fn rotate(pool: &PgPool, tenant: &str) -> Result<Rotation, RotateError> {
    if signing_key().is_none() {
        return Err(RotateError::Disabled);
    }
    if !per_tenant_key_enabled(pool, tenant).await? {
        return Err(RotateError::NotEnabled);
    }
    // Resolve the key in force from the database, not the cache.
    invalidate_tenant(pool, tenant).await;
    let old = match signing_key_for(pool, tenant).await {
        Ok(k) => k.ok_or(RotateError::Disabled)?,
        // The way out of an unusable tenant key: it cannot
        // sign its own hand-over, so the deployment key does — the key every
        // relying party of this deployment already trusts and the one that
        // signed the tenant's first hand-over. The rotation record names it
        // as `previous_kid`, so the log says what happened. Nothing else is
        // ever sealed for this tenant under the deployment key.
        Err(TenantKeyError::Unusable { kid }) => {
            tracing::warn!(
                target = "brutor::evidence",
                tenant_id = %tenant,
                unusable_kid = %kid,
                "rotating away from an unusable per-tenant evidence key; the hand-over is \
                 signed by the deployment key",
            );
            signing_key().ok_or(RotateError::Disabled)?
        }
        Err(e) => return Err(RotateError::TenantKey(e)),
    };
    let old_kid = kid_of(&old);
    let mut seed = [0u8; 32];
    rand::Rng::fill(&mut rand::rng(), &mut seed);
    let new = SigningKey::from_bytes(&seed);
    let new_kid = kid_of(&new);
    let ct = encrypt_seed(&seed).map_err(RotateError::Encryption)?;
    let m = rotation_record(tenant, &old_kid, &new.verifying_key(), chrono::Utc::now())
        .map_err(|e| RotateError::Record(e.to_string()))?;
    let sealed = crate::services::evidence::store::seal_items(
        pool,
        tenant,
        &[crate::services::evidence::store::Item::record(&m)],
        &old,
    )
    .await?;
    let s = sealed
        .into_iter()
        .next()
        .ok_or_else(|| RotateError::Record("rotation record not sealed".into()))?;
    let mut tx = pool.begin().await?;
    sqlx::query(
        "UPDATE evidence_signing_keys SET status = 'retired', retired_at = now(), \
         rotation_record_id = $2 WHERE tenant_id = $1 AND status = 'active'",
    )
    .bind(tenant)
    .bind(&s.record_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "INSERT INTO evidence_signing_keys (kid, tenant_id, encrypted_seed, status, rotation_record_id) \
         VALUES ($1, $2, $3, 'active', $4)",
    )
    .bind(&new_kid)
    .bind(tenant)
    .bind(&ct)
    .bind(&s.record_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    invalidate_tenant(pool, tenant).await;
    Ok(Rotation {
        tenant_id: tenant.to_string(),
        kid: new_kid,
        previous_kid: old_kid,
        rotation_record_id: s.record_id,
        log_id: s.log_id,
        seq: s.seq,
    })
}
