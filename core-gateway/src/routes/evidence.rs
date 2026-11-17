// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Evidence routes.
//!
//! Every route under `/v1/evidence` is system-scoped (the global service
//! key or a system admin session) — the control plane calls them over
//! `BRUTOR_GATEWAY_INTERNAL_URL`; it holds no key and does no crypto.
//!
//! * `POST /seal` — seal records at write (declarations, epochs, reports…).
//! * `POST /logs/{tenant_id}/tree-head` — force a head if anything is uncovered.
//! * `POST /verify/{record,statement,tree-head,receipt,bundle}` — pure
//!   verification; the only database reads are the trusted keys and, for
//!   `receipt` with a `witness_id`, the witness's pinned key.
//! * `POST /bundles` — build, seal and persist an evidence bundle.
//! * `POST /backfill` — seal governed rows that landed without a record.
//! * `POST /keys/{tenant_id}/rotate` — create/rotate a per-tenant key.
//! * `GET /.well-known/brutor-evidence-keys.json` — public, read-only: every
//!   key this deployment signs or signed with, and its status.
//!
//! Trust for verification = this deployment's current and retired kids
//! (`evidence_signing_keys` + the environment key + retired environment
//! keys) unless the request passes `trusted_kids`.

use crate::services::SharedState;
use crate::services::evidence::bundle::{self as bundle_svc, BundleApiError, BundleRequest};
use crate::services::evidence::store::{self, SealError};
use crate::services::evidence::{backfill, key, seal_api, witness};
use crate::services::service_auth::require_service_principal;
use axum::{
    Json, Router,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use base64::Engine as _;
use brutor_cose::cbor::as_text;
use brutor_cose::sign1::{CoseSign1, cwt, label};
use brutor_ctlog::TreeHead;
use brutor_evidence::statement::RECORD_CONTENT_TYPE;
use ed25519_dalek::VerifyingKey;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/seal", post(seal))
        .route("/logs/{tenant_id}/tree-head", post(force_tree_head))
        .route("/verify/record", post(verify_record))
        .route("/verify/statement", post(verify_statement))
        .route("/verify/tree-head", post(verify_tree_head))
        .route("/verify/receipt", post(verify_receipt))
        .route("/verify/bundle", post(verify_bundle))
        .route("/bundles", post(build_bundle))
        .route("/backfill", post(run_backfill))
        .route("/keys/{tenant_id}/rotate", post(rotate_key))
}

pub fn well_known_router() -> Router<SharedState> {
    Router::new().route("/brutor-evidence-keys.json", get(well_known_keys))
}

/// The platform service key, with system scope. Shared with the other
/// control-plane-only routes (`routes::approvals::internal_router`).
pub(crate) async fn require_system_caller(
    state: &SharedState,
    headers: &HeaderMap,
) -> Result<(), Response> {
    let (_ctx, principal) = require_service_principal(state, headers, "n/a", None).await?;
    principal.require_system_scope()
}

fn err(status: StatusCode, code: &str, detail: impl std::fmt::Display) -> Response {
    (
        status,
        Json(json!({"error": code, "detail": detail.to_string()})),
    )
        .into_response()
}

fn disabled() -> Response {
    err(
        StatusCode::SERVICE_UNAVAILABLE,
        "evidence_disabled",
        format!(
            "evidence signing key is not configured ({})",
            crate::env_vars::BRUTOR_EVIDENCE_SIGNING_KEY.name
        ),
    )
}

fn db_error(e: impl std::fmt::Display) -> Response {
    tracing::warn!(target = "brutor::evidence", error = %e, "evidence route failed");
    err(StatusCode::INTERNAL_SERVER_ERROR, "internal", e)
}

/// The tenant's own key cannot be used, so nothing is sealed.
/// The detail is fixed text: the error may name a database failure.
fn tenant_key_unavailable(e: impl std::fmt::Display) -> Response {
    tracing::error!(target = "brutor::evidence", error = %e, "tenant evidence key unavailable");
    err(
        StatusCode::SERVICE_UNAVAILABLE,
        "tenant_key_unavailable",
        "this tenant's own evidence signing key cannot be used; nothing is sealed until it \
         is rotated (POST /v1/evidence/keys/{tenant}/rotate) or ENCRYPTION_KEY is restored",
    )
}

fn seal_error(e: SealError) -> Response {
    match e {
        SealError::Disabled => disabled(),
        SealError::Db(e) => db_error(e),
        SealError::TenantKey(e) => tenant_key_unavailable(e),
    }
}

fn b64d(s: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD
        .decode(s.trim())
        .map_err(|e| e.to_string())
}

fn b64e(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

/// The trust set: the request's `trusted_kids` when given, else every kid
/// this deployment signs or signed with. `Err` is a 503: an incomplete trust
/// set would report genuine records as signed by an untrusted key.
async fn trust(
    state: &SharedState,
    requested: &Option<Vec<String>>,
) -> Result<HashSet<String>, Response> {
    match requested {
        Some(k) => Ok(k.iter().map(|x| x.trim().to_ascii_lowercase()).collect()),
        None => key::trusted_kids(&state.db)
            .await
            .map_err(|e| crate::services::db_errors::unavailable("the evidence key set", &e)),
    }
}

fn key_from_kid(kid: &[u8]) -> Option<VerifyingKey> {
    <[u8; 32]>::try_from(kid)
        .ok()
        .and_then(|a| VerifyingKey::from_bytes(&a).ok())
}

// ---------------------------------------------------------------------------
// Seal
// ---------------------------------------------------------------------------

async fn seal(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    if !key::is_enabled() {
        return disabled();
    }
    let req = match seal_api::parse(body) {
        Ok(r) => r,
        Err(e) => return err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_record", e),
    };
    match seal_api::seal(&state.db, &req).await {
        Ok(sealed) => Json(json!({
            "sealed": sealed.into_iter().map(|(s, digest)| {
                let mut v = json!({
                    "record_id": s.record_id,
                    "log_id": s.log_id,
                    "seq": s.seq,
                    "statement_b64": b64e(&s.statement),
                    "record": s.record,
                });
                if let Some(d) = digest {
                    v["payload_digest"] = json!(d);
                }
                v
            }).collect::<Vec<_>>()
        }))
        .into_response(),
        Err(seal_api::SealApiError::Invalid(m)) => {
            err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_record", m)
        }
        Err(seal_api::SealApiError::Seal(e)) => seal_error(e),
    }
}

// ---------------------------------------------------------------------------
// Tree heads
// ---------------------------------------------------------------------------

async fn force_tree_head(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    match store::force_head(&state.db, &tenant_id).await {
        Ok(Some(h)) => Json(json!({
            "issued": true,
            "head": {
                "id": h.id,
                "tree_size": h.tree_size,
                "root": hex::encode(h.root),
                "statement_b64": b64e(&h.statement),
            }
        }))
        .into_response(),
        Ok(None) => Json(json!({"issued": false, "head": null})).into_response(),
        Err(e) => seal_error(e),
    }
}

// ---------------------------------------------------------------------------
// Verify family
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct VerifyRecordBody {
    record: Value,
    #[serde(default)]
    statement_b64: Option<String>,
    /// Optional store (records in log order) for chain checks.
    #[serde(default)]
    store: Option<Vec<Value>>,
    #[serde(default)]
    trusted_kids: Option<Vec<String>>,
}

async fn verify_record(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<VerifyRecordBody>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let result = brutor_evidence::verify(&body.record, body.store.as_deref());
    let statement = match body.statement_b64.as_deref() {
        None => Value::Null,
        Some(b64) => {
            let raw = match b64d(b64) {
                Ok(r) => r,
                Err(e) => {
                    return err(StatusCode::BAD_REQUEST, "statement_b64 is not base64", e);
                }
            };
            match brutor_evidence::statement::parse_statement(&raw) {
                Err(e) => json!({"verified": false, "error": e.to_string()}),
                Ok((info, _)) => {
                    let kid = hex::encode(info.kid);
                    let trusted = match trust(&state, &body.trusted_kids).await {
                        Ok(t) => t,
                        Err(r) => return r,
                    };
                    let verified = if !trusted.contains(&kid) {
                        Err("statement kid is not a key this deployment trusts".to_string())
                    } else {
                        match key_from_kid(&info.kid) {
                            Some(k) => brutor_evidence::statement::verify_record_statement(
                                &raw,
                                &body.record,
                                &k,
                            )
                            .map(|_| true)
                            .map_err(|e| e.to_string()),
                            None => Err("kid is not an Ed25519 key".to_string()),
                        }
                    };
                    let statuses = match key::kid_statuses(&state.db).await {
                        Ok(s) => s,
                        Err(e) => {
                            return crate::services::db_errors::unavailable(
                                "the evidence key set",
                                &e,
                            );
                        }
                    };
                    json!({
                        "verified": verified.as_ref().copied().unwrap_or(false),
                        "error": verified.err(),
                        "iss": info.iss,
                        "sub": info.sub,
                        "kid": kid,
                        "kid_is_this_deployment": trusted.contains(&kid),
                        "kid_status": statuses.get(&kid),
                        "payload_record_id": info.record_id,
                    })
                }
            }
        }
    };
    let ok = result.ok
        && statement
            .get("verified")
            .and_then(Value::as_bool)
            .unwrap_or(true);
    Json(json!({
        "ok": ok,
        "findings": result.findings,
        "record_id": result.record_id,
        "effect_mode": result.effect_mode,
        "statement": statement,
    }))
    .into_response()
}

#[derive(Debug, Deserialize)]
struct VerifyStatementBody {
    statement_b64: String,
    #[serde(default)]
    record: Option<Value>,
    #[serde(default)]
    trusted_kids: Option<Vec<String>>,
}

/// Verify any Signed Statement this deployment issues (record statements
/// in hash-envelope mode, tree heads in full-content mode).
async fn verify_statement(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<VerifyStatementBody>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let trusted = match trust(&state, &body.trusted_kids).await {
        Ok(t) => t,
        Err(r) => return r,
    };
    Json(statement_result(
        &body.statement_b64,
        body.record.as_ref(),
        &trusted,
    ))
    .into_response()
}

fn statement_result(b64: &str, record: Option<&Value>, trusted: &HashSet<String>) -> Value {
    let fail = |e: String| json!({"verified": false, "error": e});
    let raw = match b64d(b64) {
        Ok(r) => r,
        Err(e) => return fail(format!("statement_b64 is not base64: {e}")),
    };
    let s = match CoseSign1::decode(&raw) {
        Ok(s) => s,
        Err(e) => return fail(format!("not a COSE_Sign1: {e}")),
    };
    let kid = s.kid().map(hex::encode).unwrap_or_default();
    let content_type = s
        .content_type()
        .or_else(|| {
            s.protected_get(label::PREIMAGE_CONTENT_TYPE)
                .and_then(as_text)
        })
        .map(str::to_owned);
    let iss = s.cwt_claim(cwt::ISS).and_then(as_text).map(str::to_owned);
    let sub = s.cwt_claim(cwt::SUB).and_then(as_text).map(str::to_owned);
    let is_record = content_type.as_deref() == Some(RECORD_CONTENT_TYPE);
    let record_id = if is_record {
        s.payload.as_ref().map(hex::encode)
    } else {
        None
    };
    let outcome: Result<(), String> = if !trusted.contains(&kid) {
        Err("statement kid is not a trusted key".into())
    } else {
        match key_from_kid(s.kid().unwrap_or_default()) {
            None => Err("kid is not an Ed25519 key".into()),
            Some(k) => match (is_record, record) {
                (true, Some(rec)) => {
                    brutor_evidence::statement::verify_record_statement(&raw, rec, &k)
                        .map(|_| ())
                        .map_err(|e| e.to_string())
                }
                (true, None) => brutor_evidence::statement::parse_statement(&raw)
                    .map_err(|e| e.to_string())
                    .and_then(|_| s.verify_ed25519(&k, None).map_err(|e| e.to_string())),
                (false, _) => s.verify_ed25519(&k, None).map_err(|e| e.to_string()),
            },
        }
    };
    json!({
        "verified": outcome.is_ok(),
        "iss": iss,
        "sub": sub,
        "kid": kid,
        "content_type": content_type,
        "record_id": record_id,
        "error": outcome.err(),
    })
}

#[derive(Debug, Deserialize)]
struct VerifyTreeHeadBody {
    statement_b64: String,
    #[serde(default)]
    previous_statement_b64: Option<String>,
    /// RFC 6962 consistency proof (hex nodes) from the previous head to
    /// this one, when the two are not directly chained.
    #[serde(default)]
    consistency_proof: Option<Vec<String>>,
    #[serde(default)]
    trusted_kids: Option<Vec<String>>,
}

fn verify_head_raw(raw: &[u8], trusted: &HashSet<String>) -> Result<TreeHead, String> {
    let s = CoseSign1::decode(raw).map_err(|e| e.to_string())?;
    let kid = s.kid().ok_or("tree head has no kid")?;
    if !trusted.contains(&hex::encode(kid)) {
        return Err("tree head kid is not a trusted key".into());
    }
    let k = key_from_kid(kid).ok_or("kid is not an Ed25519 key")?;
    TreeHead::verify(raw, &k)
        .map(|(h, _)| h)
        .map_err(|e| e.to_string())
}

async fn verify_tree_head(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<VerifyTreeHeadBody>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let trusted = match trust(&state, &body.trusted_kids).await {
        Ok(t) => t,
        Err(r) => return r,
    };
    Json(tree_head_result(&body, &trusted)).into_response()
}

fn tree_head_result(body: &VerifyTreeHeadBody, trusted: &HashSet<String>) -> Value {
    let head = b64d(&body.statement_b64).and_then(|raw| verify_head_raw(&raw, trusted));
    let head = match head {
        Ok(h) => h,
        Err(e) => return json!({"verified": false, "error": e}),
    };
    let mut out = json!({
        "verified": true,
        "log_id": head.log_id,
        "tree_size": head.tree_size,
        "root": hex::encode(head.root),
        "prev_size": head.prev_size,
        "issued_at": head.issued_at,
    });
    if let Some(prev_b64) = &body.previous_statement_b64 {
        let consistent: Result<bool, String> = b64d(prev_b64)
            .and_then(|raw| verify_head_raw(&raw, trusted))
            .and_then(|prev| {
                if prev.log_id != head.log_id {
                    return Err("previous head is for a different log".into());
                }
                if prev.tree_size > head.tree_size {
                    return Err("previous head is larger than this one".into());
                }
                if prev.tree_size == head.tree_size {
                    return Ok(prev.root == head.root);
                }
                if head.prev_size == prev.tree_size {
                    // The embedded proof (checked by TreeHead::verify) runs
                    // from prev_root; it must be the previous head's root.
                    return Ok(head.prev_root == Some(prev.root));
                }
                let proof = body
                    .consistency_proof
                    .as_ref()
                    .ok_or("heads are not adjacent; supply consistency_proof")?;
                let path: Option<Vec<[u8; 32]>> = proof
                    .iter()
                    .map(|h| hex::decode(h).ok().and_then(|b| b.try_into().ok()))
                    .collect();
                let path = path.ok_or("consistency_proof is not hex SHA-256 nodes")?;
                Ok(brutor_cose::merkle::verify_consistency(
                    prev.tree_size,
                    &prev.root,
                    head.tree_size,
                    &head.root,
                    &path,
                )
                .is_ok())
            });
        match consistent {
            Ok(c) => out["consistent_with_previous"] = json!(c),
            Err(e) => {
                out["consistent_with_previous"] = Value::Null;
                out["error"] = json!(e);
            }
        }
    }
    out
}

#[derive(Debug, Deserialize)]
struct VerifyReceiptBody {
    receipt_b64: String,
    statement_b64: String,
    #[serde(default)]
    pinned_key_hex: Option<String>,
    /// PEM certificate or SHA-256 fingerprint for an RFC 3161 token.
    #[serde(default)]
    pinned_key: Option<String>,
    #[serde(default)]
    witness_id: Option<String>,
    /// `scitt` | `rfc3161`; detected when absent.
    #[serde(default)]
    kind: Option<String>,
}

async fn verify_receipt(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<VerifyReceiptBody>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let (receipt, statement) = match (b64d(&body.receipt_b64), b64d(&body.statement_b64)) {
        (Ok(r), Ok(s)) => (r, s),
        _ => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_request",
                "receipt_b64 and statement_b64 must be base64",
            );
        }
    };
    let mut kind = body.kind.clone();
    let mut pin = body.pinned_key_hex.clone().or(body.pinned_key.clone());
    if let Some(wid) = &body.witness_id {
        let row: Result<Option<(String, Option<String>)>, sqlx::Error> =
            sqlx::query_as("SELECT kind, pinned_key FROM witnesses WHERE id = $1")
                .bind(wid)
                .fetch_optional(&state.db)
                .await;
        let row = match row {
            Ok(r) => r,
            // Not "unknown witness".
            Err(e) => return crate::services::db_errors::unavailable("the witness", &e),
        };
        match row {
            Some((k, p)) => {
                kind.get_or_insert(k);
                if pin.is_none() {
                    pin = p;
                }
            }
            None => return err(StatusCode::NOT_FOUND, "unknown_witness", wid),
        }
    }
    let kind = kind.unwrap_or_else(|| {
        if CoseSign1::decode(&receipt).is_ok() {
            "scitt".into()
        } else {
            "rfc3161".into()
        }
    });
    let g = match kind.as_str() {
        "scitt" => witness::grade_scitt(&receipt, &statement, pin.as_deref()),
        "rfc3161" => witness::grade_rfc3161(&receipt, &statement, pin.as_deref()),
        other => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_request",
                format!("unknown receipt kind {other:?}"),
            );
        }
    };
    Json(json!({
        "grade": g.verdict,
        "kind": kind,
        "tree_size": g.tree_size,
        "leaf_index": g.leaf_index,
        "iat": g.iat,
        "vendor_ext": g.vendor_ext.unwrap_or(Value::Null),
        "errors": g.errors,
    }))
    .into_response()
}

#[derive(Debug, Deserialize)]
struct VerifyBundleBody {
    bundle: Value,
    #[serde(default)]
    bundle_digest: Option<String>,
    #[serde(default)]
    trusted_kids: Option<Vec<String>>,
}

async fn verify_bundle(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<VerifyBundleBody>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let trusted = match trust(&state, &body.trusted_kids).await {
        Ok(t) => t,
        Err(r) => return r,
    };
    let v = brutor_evidence::bundle::verify(
        &body.bundle,
        &brutor_evidence::bundle::VerifyOptions {
            trusted_kids: Some(trusted),
            expected_digest: body.bundle_digest.clone(),
        },
    );
    Json(json!(v)).into_response()
}

// ---------------------------------------------------------------------------
// Bundles, backfill, keys
// ---------------------------------------------------------------------------

async fn build_bundle(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let req: BundleRequest = match serde_path_to_error::deserialize(body) {
        Ok(r) => r,
        Err(e) => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_request",
                format!("{}: {}", e.path(), e.inner()),
            );
        }
    };
    match bundle_svc::build(&state.db, &req).await {
        Ok(b) => {
            let mut v = json!({
                "bundle_id": b.bundle_id,
                "bundle_digest": b.bundle_digest,
                "record_id": b.record_id,
                "bundle": b.bundle,
                "persisted": b.persisted,
            });
            if let Some(d) = b.disclosure_id {
                v["disclosure_id"] = json!(d);
            }
            Json(v).into_response()
        }
        Err(BundleApiError::Invalid(m)) => {
            err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request", m)
        }
        Err(BundleApiError::PayloadsUnavailable) => err(
            StatusCode::CONFLICT,
            "payloads_unavailable",
            "record bodies never enter evidence records; only payloads: \"none\" can be met",
        ),
        Err(BundleApiError::TooLarge(m)) => {
            err(StatusCode::PAYLOAD_TOO_LARGE, "bundle_too_large", m)
        }
        Err(BundleApiError::Empty) => err(
            StatusCode::NOT_FOUND,
            "no_records",
            "no records match the selector",
        ),
        Err(BundleApiError::Seal(e)) => seal_error(e),
        Err(BundleApiError::Db(e)) => db_error(e),
        Err(BundleApiError::Internal(m)) => db_error(m),
    }
}

async fn run_backfill(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    let req: backfill::BackfillRequest = match serde_path_to_error::deserialize(body) {
        Ok(r) => r,
        Err(e) => {
            return err(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_request",
                format!("{}: {}", e.path(), e.inner()),
            );
        }
    };
    match backfill::run(&state.db, &req).await {
        Ok(report) => Json(json!(report)).into_response(),
        Err(backfill::BackfillError::Invalid(m)) => {
            err(StatusCode::UNPROCESSABLE_ENTITY, "invalid_request", m)
        }
        Err(backfill::BackfillError::InProgress) => err(
            StatusCode::CONFLICT,
            "backfill_in_progress",
            "a backfill for this tenant is already running",
        ),
        Err(backfill::BackfillError::Seal(e)) => seal_error(e),
        Err(backfill::BackfillError::Db(e)) => db_error(e),
    }
}

async fn rotate_key(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    if let Err(r) = require_system_caller(&state, &headers).await {
        return r;
    }
    key::install_encryption(state.encryption.clone());
    match key::rotate(&state.db, &tenant_id).await {
        Ok(r) => Json(json!(r)).into_response(),
        Err(key::RotateError::Disabled) => disabled(),
        Err(key::RotateError::NotEnabled) => err(
            StatusCode::CONFLICT,
            "per_tenant_key_disabled",
            "enable evidence_settings.per_tenant_key for this tenant first",
        ),
        Err(key::RotateError::Seal(e)) => seal_error(e),
        Err(key::RotateError::TenantKey(e)) => tenant_key_unavailable(e),
        Err(e) => db_error(e),
    }
}

async fn well_known_keys(State(state): State<SharedState>) -> Response {
    // A failed read is a 503 with no public cache header, never a
    // key list missing the tenant keys that clients would cache.
    let doc = match key::well_known_document_with_tenants(&state.db).await {
        Ok(d) => d,
        Err(e) => return crate::services::db_errors::unavailable("the evidence key set", &e),
    };
    let mut resp = Json(doc).into_response();
    resp.headers_mut().insert(
        axum::http::header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("public, max-age=300"),
    );
    resp
}

#[cfg(test)]
mod tests {
    use super::*;
    use brutor_ctlog::TreeHead;
    use ed25519_dalek::SigningKey;

    fn trusted(k: &SigningKey) -> HashSet<String> {
        [hex::encode(k.verifying_key().to_bytes())]
            .into_iter()
            .collect()
    }

    fn head(
        size: u64,
        prev: Option<(u64, [u8; 32])>,
        proof: Vec<[u8; 32]>,
        root: [u8; 32],
    ) -> TreeHead {
        TreeHead {
            log_id: "log-x".into(),
            tree_size: size,
            root,
            prev_size: prev.map(|p| p.0).unwrap_or(0),
            prev_root: prev.map(|p| p.1),
            issued_at: "2026-09-19T00:00:00.000Z".into(),
            consistency_proof: proof,
        }
    }

    #[test]
    fn tree_head_verification_and_consistency() {
        use brutor_cose::merkle::{consistency_proof, leaf_hash, tree_hash};
        let k = SigningKey::from_bytes(&[3u8; 32]);
        let leaves: Vec<[u8; 32]> = (0..9u8).map(|i| leaf_hash(&[i])).collect();
        let h3 = head(3, None, vec![], tree_hash(&leaves[..3]));
        let h5 = head(
            5,
            Some((3, h3.root)),
            consistency_proof(3, &leaves[..5]),
            tree_hash(&leaves[..5]),
        );
        let h9 = head(
            9,
            Some((5, h5.root)),
            consistency_proof(5, &leaves),
            tree_hash(&leaves),
        );
        let s = |h: &TreeHead| b64e(&h.sign(&k).unwrap());
        let body = |cur: &TreeHead, prev: Option<&TreeHead>, proof: Option<Vec<String>>| {
            VerifyTreeHeadBody {
                statement_b64: s(cur),
                previous_statement_b64: prev.map(s),
                consistency_proof: proof,
                trusted_kids: None,
            }
        };
        let r = tree_head_result(&body(&h5, Some(&h3), None), &trusted(&k));
        assert_eq!(r["verified"], true);
        assert_eq!(r["consistent_with_previous"], true);
        // Not adjacent: needs a proof.
        let r = tree_head_result(&body(&h9, Some(&h3), None), &trusted(&k));
        assert!(r["consistent_with_previous"].is_null());
        let proof: Vec<String> = consistency_proof(3, &leaves)
            .iter()
            .map(hex::encode)
            .collect();
        let r = tree_head_result(&body(&h9, Some(&h3), Some(proof)), &trusted(&k));
        assert_eq!(r["consistent_with_previous"], true);
        // A fork: a different 3-leaf history is not consistent.
        let fork = head(
            3,
            None,
            vec![],
            tree_hash(&[leaves[1], leaves[0], leaves[2]]),
        );
        let r = tree_head_result(&body(&h5, Some(&fork), None), &trusted(&k));
        assert_eq!(r["consistent_with_previous"], false);
        // Untrusted signer.
        let r = tree_head_result(&body(&h5, None, None), &HashSet::new());
        assert_eq!(r["verified"], false);
    }

    #[test]
    fn statement_verification_reports_kind_and_identity() {
        use brutor_evidence::record::{Disposition, RecordBuilder};
        let k = SigningKey::from_bytes(&[4u8; 32]);
        let mut b = RecordBuilder::new("x/1", "t", "2026-09-19T00:00:00.000Z");
        b.verdict = "recorded".into();
        b.kind = "fyi".into();
        b.disposition = Some(Disposition {
            decision: "accept".into(),
            approver: "policy".into(),
            human_disposed: false,
            authority: None,
            reason_digest: None,
        });
        let (rec, id) = b.seal().unwrap();
        let st = brutor_evidence::statement::seal_record(&rec, "urn:x", "tenant:t", &k).unwrap();
        let r = statement_result(&b64e(&st), Some(&rec), &trusted(&k));
        assert_eq!(r["verified"], true, "{r}");
        assert_eq!(r["record_id"], id);
        assert_eq!(r["content_type"], RECORD_CONTENT_TYPE);
        let mut other = rec.clone();
        other["action_id"] = json!("x/2");
        assert_eq!(
            statement_result(&b64e(&st), Some(&other), &trusted(&k))["verified"],
            false
        );
        assert_eq!(
            statement_result(&b64e(&st), None, &HashSet::new())["verified"],
            false
        );
        let th = head(1, None, vec![], [1u8; 32]).sign(&k).unwrap();
        let r = statement_result(&b64e(&th), None, &trusted(&k));
        assert_eq!(r["verified"], true);
        assert_eq!(r["content_type"], brutor_ctlog::TREE_HEAD_CONTENT_TYPE);
        assert!(r["record_id"].is_null());
    }
}
