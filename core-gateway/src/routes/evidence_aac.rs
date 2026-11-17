// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Optional Agent Action Capsule export.
//!
//! `GET /v1/evidence/records/{record_id}/aac` — system-scoped (service key
//! or system admin, the same gate as `POST /v1/evidence/verify/record`).
//! Returns the deterministic AAC `-04` Capsule for a sealed action record
//! plus a Producer Envelope signed with the key that tenant's records are
//! signed with. The profile is **off** unless `BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED=true`;
//! when off the route answers 404 before authenticating, so its existence
//! is not observable.
//!
//! Read-only: the Capsule is a projection computed on demand. Nothing is
//! written and the stored record model is untouched. A chained record is
//! mapped with its lineage (parent Capsule IDs are themselves derived from
//! the parents' records), walked within the record's own tenant.

use crate::services::SharedState;
use crate::services::evidence::key;
use crate::services::service_auth::require_service_principal;
use axum::{
    Json, Router,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
};
use base64::Engine as _;
use serde_json::{Value, json};

/// Relative to `/v1/evidence`; merged into that nest in `main.rs`.
pub fn router() -> Router<SharedState> {
    Router::new().route("/records/{record_id}/aac", get(record_capsule))
}

fn error(status: StatusCode, code: &str, detail: impl Into<String>) -> Response {
    (
        status,
        Json(json!({"error": code, "detail": detail.into()})),
    )
        .into_response()
}

async fn load_record(
    state: &SharedState,
    record_id: &str,
    tenant: Option<&str>,
) -> Result<Option<(String, Value)>, sqlx::Error> {
    let row: Option<(String, Value)> = match tenant {
        None => {
            sqlx::query_as("SELECT tenant_id, record FROM action_records WHERE record_id = $1")
                .bind(record_id)
                .fetch_optional(&state.db)
                .await?
        }
        Some(t) => sqlx::query_as(
            "SELECT tenant_id, record FROM action_records WHERE record_id = $1 AND tenant_id = $2",
        )
        .bind(record_id)
        .bind(t)
        .fetch_optional(&state.db)
        .await?,
    };
    Ok(row)
}

/// Whether the profile is served (`BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED`).
pub fn profile_enabled() -> bool {
    crate::env_vars::process().flag(&crate::env_vars::BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED)
}

async fn record_capsule(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Path(record_id): Path<String>,
) -> Response {
    if !profile_enabled() {
        return StatusCode::NOT_FOUND.into_response();
    }
    let (_ctx, principal) = match require_service_principal(&state, &headers, "n/a", None).await {
        Ok(p) => p,
        Err(r) => return r,
    };
    if let Err(r) = principal.require_system_scope() {
        return r;
    }
    if !brutor_aac::canon::is_hex64(&record_id) {
        return error(
            StatusCode::BAD_REQUEST,
            "invalid_record_id",
            "record_id is 64 lowercase hex",
        );
    }

    let (tenant, record) = match load_record(&state, &record_id, None).await {
        Ok(Some(r)) => r,
        Ok(None) => return error(StatusCode::NOT_FOUND, "record_not_found", record_id),
        Err(e) => {
            tracing::warn!(target = "brutor::evidence", error = %e, "aac: record lookup failed");
            return error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "db_error",
                "record lookup failed",
            );
        }
    };

    // Walk the chain up to its root, inside the same tenant.
    let mut lineage = vec![record];
    loop {
        let parent = lineage
            .last()
            .and_then(|r| r.get("chain"))
            .and_then(|c| c.get("parent_record_id"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let Some(parent) = parent else { break };
        if lineage.len() >= brutor_aac::mapping::MAX_LINEAGE {
            return error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "lineage_too_deep",
                "chain exceeds the mapping depth limit",
            );
        }
        match load_record(&state, &parent, Some(&tenant)).await {
            Ok(Some((_, r))) => lineage.push(r),
            Ok(None) => {
                return error(
                    StatusCode::CONFLICT,
                    "parent_record_unavailable",
                    format!(
                        "chain parent {parent} is not available; the Capsule's chain cannot be derived"
                    ),
                );
            }
            Err(e) => {
                tracing::warn!(target = "brutor::evidence", error = %e, "aac: parent lookup failed");
                return error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "db_error",
                    "record lookup failed",
                );
            }
        }
    }
    lineage.reverse();

    let mapped = match brutor_aac::map_lineage(&lineage) {
        Ok(m) => m,
        Err(e) => {
            return error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "not_mappable",
                e.to_string(),
            );
        }
    };
    let Some(target) = mapped.last() else {
        return error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "not_mappable",
            "empty lineage",
        );
    };
    let capsules: Vec<Value> = mapped.iter().map(|m| m.capsule.clone()).collect();
    let class1 = brutor_aac::verify_store(&capsules).pop();

    let envelope = match key::signing_key_for(&state.db, &tenant).await {
        Ok(None) => {
            json!({"envelope_b64": null, "error": "evidence signing not configured on this deployment"})
        }
        // Not signed under another key.
        Err(_) => {
            json!({"envelope_b64": null, "error": "this tenant's evidence signing key is unavailable"})
        }
        Ok(Some(k)) => match brutor_aac::sign_producer_envelope(&target.capsule_id, &k) {
            Ok(raw) => json!({
                "envelope_b64": base64::engine::general_purpose::STANDARD.encode(raw),
                "kid": hex::encode(k.verifying_key().as_bytes()),
                "content_type": brutor_aac::CAPSULE_ID_MEDIA_TYPE,
            }),
            Err(e) => json!({"envelope_b64": null, "error": e.to_string()}),
        },
    };

    Json(json!({
        "profile": brutor_aac::SPEC_VERSION,
        "mapping": brutor_aac::mapping::MAPPING_ID,
        "record_id": target.record_id,
        "capsule_id": target.capsule_id,
        "capsule": target.capsule,
        "lossy": target.lossy,
        "producer_envelope": envelope,
        "class1": class1,
        "lineage": mapped.iter().map(|m| json!({"record_id": m.record_id, "capsule_id": m.capsule_id})).collect::<Vec<_>>(),
    }))
    .into_response()
}
