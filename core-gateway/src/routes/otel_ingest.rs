// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `POST /v1/ingest/otel/v1/traces` — the OpenTelemetry GenAI inlet.
//!
//! An OTLP/HTTP endpoint a collector or SDK exporter can point at directly.
//! Authenticated with a gateway API key; the AI System is attributed from
//! `X-Brutor-AI-System`, the `brutor.ai_system` span/resource attribute, or
//! the key's own resource group, through the same resolver every proxied
//! call uses. See `services::otel_ingest` for what the rows mean.

use std::collections::BTreeSet;

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::post,
};
use serde::Serialize;

use crate::middleware::tenant::TenantContext;
use crate::routes::route_helpers;
use crate::services::{
    SharedState,
    ai_system::{AI_SYSTEM_HEADER, AttributionInputs, resolve_ai_system},
    auth,
    otel_ingest::{self, IngestContext, ParseError},
};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/v1/traces", post(export_traces))
        .layer(DefaultBodyLimit::max(otel_ingest::MAX_BODY_BYTES))
}

/// OTLP's own partial-success envelope, so a collector treats the inlet
/// like any other OTLP endpoint.
#[derive(Debug, Serialize, utoipa::ToSchema)]
#[serde(rename_all = "camelCase")]
pub struct PartialSuccess {
    pub rejected_spans: u64,
    pub error_message: String,
}

#[derive(Debug, Serialize, utoipa::ToSchema)]
#[serde(rename_all = "camelCase")]
pub struct ExportResponse {
    pub partial_success: PartialSuccess,
    /// GenAI spans turned into step rows.
    pub accepted: usize,
    /// Spans in the export that were not GenAI evidence (kept as topology).
    pub skipped: usize,
    /// Run ids (`otel:<trace id>`) the accepted spans belong to.
    pub runs: Vec<String>,
    /// The AI System the runs were attributed to, if any.
    pub ai_system_id: Option<String>,
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

#[utoipa::path(
    post,
    path = "/v1/ingest/otel/v1/traces",
    tag = "Ingest",
    summary = "Ingest OpenTelemetry GenAI spans as run evidence",
    description = "OTLP/HTTP **JSON** `ExportTraceServiceRequest`. Spans carrying GenAI \
                   semantic-convention attributes (`gen_ai.operation.name` = chat | \
                   text_completion | generate_content | embeddings | execute_tool | \
                   invoke_agent) become step rows of a run keyed on the trace id; every \
                   other span is skipped. Ingested evidence is recorded as *reported* \
                   (`source = otel`, `chain_integrity = observed_external`): the gateway did \
                   not witness it and applied no control. Attribute the run with \
                   `X-Brutor-AI-System` or the `brutor.ai_system` resource attribute. \
                   Protobuf bodies are refused with 415.",
    request_body(content = serde_json::Value, content_type = "application/json"),
    responses(
        (status = 200, description = "Export accepted", body = ExportResponse),
        (status = 400, description = "Body is not an OTLP JSON export"),
        (status = 401, description = "Missing or invalid API key"),
        (status = 413, description = "Too many spans in one request"),
        (status = 415, description = "Protobuf is not accepted; use http/json"),
    ),
)]
pub(crate) async fn export_traces(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    body: Bytes,
) -> Response {
    let tenant_id = match route_helpers::extract_tenant_id(tenant) {
        Ok(t) => t,
        Err(r) => return r,
    };

    // Header-only shape check before auth: reading one header costs nothing
    // and gives an exporter a precise message instead of a 401 it will misread
    // as a key problem.
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if content_type.contains("protobuf") {
        return error_response(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "OTLP/HTTP JSON only: set the exporter protocol to http/json (Content-Type: application/json); protobuf is not accepted",
        );
    }

    // Authenticate BEFORE parsing the body.
    //
    // The JSON parse and `parse_export` walk of every resource, scope and span
    // used to run first, justified as "shape checks cost nothing". That is
    // true of the content-type header above and false of an 8 MB document with
    // up to 5 000 spans: the tenant layer requires no tenant of this route, so an
    // unauthenticated caller needed only an `X-Tenant-ID` header to make the
    // gateway parse arbitrary JSON on their behalf, as often as they liked.
    // Work done for an unauthenticated caller is the whole attack.
    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return crate::services::identity_policy::refusal_response(
            &e,
            error_response(e.status_code(), &e.to_string()),
        )
        .await;
    }

    let value: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            return error_response(StatusCode::BAD_REQUEST, &format!("body is not JSON: {e}"));
        }
    };
    let export = match otel_ingest::parse_export(&value) {
        Ok(e) => e,
        Err(e @ ParseError::NotAnExport) => {
            return error_response(StatusCode::BAD_REQUEST, &e.to_string());
        }
        Err(e @ ParseError::TooManySpans(_)) => {
            return error_response(StatusCode::PAYLOAD_TOO_LARGE, &e.to_string());
        }
    };

    // Header first, then whatever the exporter put on its spans/resource.
    // Either way the claim is validated by the resolver — an exporter can
    // no more attribute to a system its key cannot reach than a client can.
    let header_claim = headers
        .get(AI_SYSTEM_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .or_else(|| export.spans.iter().find_map(|s| s.ai_system_claim.clone()));
    let attribution = resolve_ai_system(
        &state.db,
        &tenant_id,
        &AttributionInputs {
            header_claim: header_claim.as_deref(),
            api_key_group_id: auth_ctx.api_key_group_id.as_deref(),
            ..Default::default()
        },
    )
    .await;
    // Spans attributed from a failed lookup would be unattributed
    // (or a valid claim recorded as rejected); the exporter retries a 503.
    let attribution = match attribution {
        Ok(a) => a,
        Err(e) => return crate::services::db_errors::unavailable("AI System attribution", &e),
    };

    let correlation_id = format!("otel-{}", ulid::Ulid::new());
    let ctx = IngestContext {
        auth: &auth_ctx,
        ai_system_id: attribution.ai_system_id.as_deref(),
        correlation_id: &correlation_id,
    };

    let mut runs: BTreeSet<String> = BTreeSet::new();
    for span in &export.spans {
        let log = otel_ingest::span_to_log(span, &export, &ctx);
        if let Some(root) = &log.root_task_id {
            runs.insert(root.clone());
        }
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);
    }

    tracing::info!(
        tenant_id = %tenant_id,
        accepted = export.spans.len(),
        skipped = export.skipped,
        runs = runs.len(),
        ai_system_id = ?attribution.ai_system_id,
        rejected_claim = ?attribution.rejected_claim,
        "otel inlet: export ingested"
    );

    (
        StatusCode::OK,
        Json(ExportResponse {
            partial_success: PartialSuccess {
                rejected_spans: 0,
                error_message: String::new(),
            },
            accepted: export.spans.len(),
            skipped: export.skipped,
            runs: runs.into_iter().collect(),
            ai_system_id: attribution.ai_system_id,
        }),
    )
        .into_response()
}
