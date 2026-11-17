// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Audit-chain verification endpoint.
//!
//! Called by the **control plane**, not by end users, over the internal
//! `BRUTOR_GATEWAY_INTERNAL_URL` path the admin API already uses for model health
//! checks and cache invalidation. Verification recomputes the canonical row
//! bytes, so it has to live where that function lives; a second copy of the
//! format in Python would drift, and honest rows would start failing
//! verification with no way to tell that apart from a real tamper.
//!
//! ## Why this is not tenant-scoped
//!
//! A hash chain belongs to a *writer process*, and one chain interleaves rows
//! from every tenant that process served. There is no meaningful per-tenant
//! view of it: a tenant admin verifying a chain would learn the sequence
//! ranges, and therefore the traffic volume, of everyone else on the same
//! proxy.
//!
//! So these routes are **system-admin only**, enforced on the control-plane
//! side where admin identity lives, and enforced again here: every route
//! requires the control plane's global-scope service key or a system admin
//! session (`services::service_auth`). Before that gate existed the routes
//! were reachable by anyone on the proxy port, which made
//! `export-manifest/sign` a signing oracle for forged evidence. They deliberately return no row contents — only counts,
//! sequence numbers and verdicts — so even the internal surface leaks nothing
//! about what any tenant did.

use axum::extract::{Path, Query, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;

use crate::routes::route_helpers::openai_error;
use crate::services::SharedState;
use crate::services::audit_chain::{self, ExportManifest};
use crate::services::audit_verify::{self, DEFAULT_SCAN_LIMIT};
use crate::services::service_auth::require_service_principal;

/// System-scoped gate shared by every route in this module.
async fn require_system_caller(state: &SharedState, headers: &HeaderMap) -> Result<(), Response> {
    let (_ctx, principal) = require_service_principal(state, headers, "n/a", None).await?;
    principal.require_system_scope()
}

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/chains", get(list_chains))
        .route("/chains/{chain_id}/verify", post(verify))
        .route("/export-manifest/sign", post(sign_export_manifest))
}

/// `POST /v1/audit/export-manifest/sign` — sign an export manifest.
///
/// Deliberately **not** a "sign these bytes" endpoint. A general signing
/// oracle would let anyone who reached it have the audit key sign anything,
/// including a chain checkpoint — which is precisely the forgery the trust
/// anchor exists to prevent. This accepts the manifest's fields and signs a
/// canonical serialisation of them, so the key can only ever produce the two
/// shapes the verifier understands.
///
/// The control plane calls this because the signing key lives here, with the
/// rest of the audit-chain machinery, rather than being distributed to every
/// service that might want an attestation.
async fn sign_export_manifest(
    State(state): State<SharedState>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    // Authenticate before parsing: a `Json<_>` extractor would answer 422
    // to an anonymous caller with a malformed body, which both leaks the
    // schema and reorders the contract (credentials first, always).
    if let Err(resp) = require_system_caller(&state, &headers).await {
        return resp;
    }
    let manifest: SignableManifest = match serde_json::from_slice(&body) {
        Ok(m) => m,
        Err(_) => {
            return openai_error(
                axum::http::StatusCode::UNPROCESSABLE_ENTITY,
                "body is not an export or report manifest",
            );
        }
    };
    // Still not a signing oracle: two fixed shapes, each with its own
    // domain-tagged canonical serialisation, and nothing else. A report
    // manifest whose `kind` is not the pinned spec id is refused — the key
    // signs shapes we defined, never shapes the caller describes.
    let signed = match &manifest {
        SignableManifest::Export(m) => audit_chain::sign_manifest(m),
        SignableManifest::Report(m) => {
            if m.kind != audit_chain::REPORT_MANIFEST_KIND {
                return openai_error(
                    axum::http::StatusCode::UNPROCESSABLE_ENTITY,
                    "unknown_manifest_kind",
                );
            }
            audit_chain::sign_report_manifest(m)
        }
    };
    match signed {
        Some((signature, public_key)) => Json(serde_json::json!({
            "signature": signature,
            "public_key": public_key,
            // The tag inside the signed manifest bytes — the signed-shape
            // version, not the row format (the two are separate).
            "canonical_version": audit_chain::SIGNED_SHAPE_VERSION,
        }))
        .into_response_ok(),
        None => {
            // No key configured. A 501 rather than a 500: nothing failed, the
            // deployment simply has not opted into signing, and the caller
            // should record an unsigned digest rather than retry.
            tracing::debug!("export manifest signing requested but no audit key is configured");
            openai_error(
                axum::http::StatusCode::NOT_IMPLEMENTED,
                "audit_signing_not_configured",
            )
        }
    }
}

/// The two attestation shapes the audit key will produce. Untagged: an
/// export manifest carries `object_key`/`byte_size`/… and a report manifest
/// carries `report_sha256`/`kind` — the field sets are disjoint enough that
/// a document can only ever match one arm.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum SignableManifest {
    Export(ExportManifest),
    Report(audit_chain::ReportManifest),
}

#[derive(Debug, Deserialize, Default)]
pub struct VerifyQuery {
    /// First sequence to check. Lets a caller walk a long chain in pages
    /// rather than asking for an unbounded scan.
    #[serde(default)]
    pub from_seq: Option<i64>,
    pub limit: Option<i64>,
}

#[derive(Debug, Deserialize, Default)]
pub struct ListQuery {
    pub limit: Option<i64>,
}

/// `GET /v1/audit/chains` — chains present in `proxy_logs`.
async fn list_chains(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Query(q): Query<ListQuery>,
) -> Response {
    if let Err(resp) = require_system_caller(&state, &headers).await {
        return resp;
    }
    match audit_verify::list_chains(&state.db, q.limit.unwrap_or(100)).await {
        Ok(chains) => Json(serde_json::json!({ "chains": chains })).into_response_ok(),
        Err(e) => {
            tracing::warn!(error = %e, "audit chain listing failed");
            openai_error(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "audit_chain_listing_failed",
            )
        }
    }
}

/// `POST /v1/audit/chains/{chain_id}/verify` — walk a range and report.
///
/// A failure to *run* the check is a 500, deliberately distinct from a check
/// that ran and found a problem (200 with a `broken` verdict). Collapsing the
/// two would let an outage read as a clean bill of health.
async fn verify(
    State(state): State<SharedState>,
    headers: HeaderMap,
    Path(chain_id): Path<String>,
    Query(q): Query<VerifyQuery>,
) -> Response {
    if let Err(resp) = require_system_caller(&state, &headers).await {
        return resp;
    }
    let from = q.from_seq.unwrap_or(0);
    let limit = q.limit.unwrap_or(DEFAULT_SCAN_LIMIT);

    match audit_verify::verify_chain(&state.db, &chain_id, from, limit).await {
        Ok(report) => {
            if !report.verdict.is_intact() {
                // An operator-visible signal on anything short of a clean
                // verdict: a chain that has been tampered with is not a
                // reporting detail, and it must not be discoverable only by
                // whoever happened to open the page.
                tracing::warn!(
                    chain_id = %report.chain_id,
                    verdict = %report.verdict.as_str(),
                    rows = report.rows_checked,
                    first_bad_seq = ?report.first_bad_seq,
                    gaps = report.gaps.len(),
                    "audit chain verification did not return a clean verdict",
                );
            }
            Json(report).into_response_ok()
        }
        Err(e) => {
            tracing::error!(error = %e, chain_id = %chain_id, "audit chain verification failed to run");
            openai_error(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "audit_verification_failed",
            )
        }
    }
}

/// Small helper so both handlers return the same shape without repeating the
/// `IntoResponse` dance.
trait IntoOkResponse {
    fn into_response_ok(self) -> Response;
}

impl<T: serde::Serialize> IntoOkResponse for Json<T> {
    fn into_response_ok(self) -> Response {
        use axum::response::IntoResponse;
        self.into_response()
    }
}
