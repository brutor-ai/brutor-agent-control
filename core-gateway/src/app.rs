// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Application assembly
//!
//! The gateway's HTTP application: every route the proxy serves and the
//! middleware stack around them, built from one [`SharedState`]. The binary
//! (`main.rs`) builds the state, calls [`router`], and serves the result; it
//! holds no routes of its own.
//!
//! The assembly lives in the library, not in the binary, so that it has one
//! definition that both the binary and a test can build, and so that another
//! distribution can add to it.
//!
//! Layer order is behaviour. Tower applies layers outside-in in the reverse of
//! the order they are added here: the last `.layer` call is the outermost.
//! Each layer below says why it sits where it does.

use axum::Router;
use tower_http::trace::{DefaultOnFailure, TraceLayer};
use tracing::Level;

use crate::services::SharedState;
use crate::{middleware, openapi, routes};

/// The public application: all routes, wrapped in the middleware stack.
///
/// Fails only if the API document cannot be built (see
/// [`openapi::docs_router`]).
pub fn router(state: SharedState) -> anyhow::Result<Router> {
    router_with(state, Vec::new(), openapi::document()?)
}

/// [`router`] with an extension's routes merged after the built-in ones and
/// the OpenAPI document given (the built-in one with the extensions'
/// fragments). The extension routes sit inside the middleware
/// stack like every built-in route.
pub fn router_with(
    state: SharedState,
    extension_routes: Vec<Router<SharedState>>,
    document: serde_json::Value,
) -> anyhow::Result<Router> {
    let mut extensions = Router::new();
    for routes in extension_routes {
        extensions = extensions.merge(routes);
    }
    let app = Router::new()
        .nest("/v1", routes::anthropic::router())
        // Gemini native client surface. Merged (not nested) at root
        // because Google's URL convention bakes its own `/v1beta`
        // version prefix into the route — nesting would double it.
        .merge(routes::gemini::router())
        .nest("/v1/proxy/llm", routes::llm::router())
        .nest("/v1/proxy/mcp", routes::mcp::router())
        .nest("/v1/proxy/vmcp", routes::vmcp::router())
        .nest("/v1/proxy/skills", routes::skills::router())
        .nest("/v1/proxy/files", routes::files::router())
        .nest("/v1/proxy/llm/images/generations", routes::images::router())
        .nest("/v1/proxy/llm/audio/speech", routes::audio_speech::router())
        .nest(
            "/v1/proxy/llm/audio/transcriptions",
            routes::audio_transcribe::router(),
        )
        .nest("/v1/proxy/llm/video/generations", routes::video::router())
        .nest("/v1/proxy/llm/video/jobs", routes::video::jobs_router())
        .nest("/v1/proxy/a2a", routes::a2a::router())
        .nest("/a2a", routes::a2a_v1::router())
        .merge(routes::a2a::discovery_router())
        .nest("/v1/proxy/oauth", routes::oauth::router())
        // Explicit run finalization + inspection.
        .nest("/v1/audit", routes::audit::router())
        // Sealed action-record verification (CP-called) and the
        // public evidence-key document a relying party pins out of band.
        // The optional AAC profile export rides in the same nest;
        // it answers 404 unless BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED=true.
        .nest(
            "/v1/evidence",
            routes::evidence::router().merge(routes::evidence_aac::router()),
        )
        .nest("/.well-known", routes::evidence::well_known_router())
        .nest("/v1/runs", routes::runs::router())
        // OpenTelemetry GenAI inlet: reported evidence, kept apart from witnessed.
        .nest("/v1/ingest/otel", routes::otel_ingest::router())
        // CP-called compile/backtest for tier-A checks.
        .nest("/v1/assurance", routes::assurance_checks::router())
        // Tool approvals: Community, outside the portal prefix.
        // Agents poll `/v1/approvals/{id}/poll`; the decision routes take a
        // portal session (a credential only where the Portal extension is
        // composed). The tenant layer blocks the prefix for a suspended
        // tenant (`services::tenant_paths`).
        .nest("/v1/approvals", routes::approvals::router())
        // The control plane decides on an administrator's behalf.
        // Platform service key only; outside `/v1/approvals/` so the
        // tenant layer's runtime block (keyed on the *key's* tenant) does
        // not apply to a call made for another tenant.
        .nest(
            "/v1/internal/approvals",
            routes::approvals::internal_router(),
        )
        .nest("/v1/assets", routes::icons::router())
        // The filesystem media store's signed download links: the link is
        // the credential (`routes::media`).
        .nest("/v1/media", routes::media::router())
        .merge(routes::gateway::router())
        // An extension's routes, inside the same middleware stack.
        .merge(extensions)
        // Swagger UI + the document (see `openapi::docs_router`).
        .merge(openapi::docs_router(document))
        // No `/metrics` here: it is served on its own listener, from
        // `metrics_router`.
        // tower-http's `TraceLayer` defaults to ERROR-level logging
        // for any 5xx response, which conflicts with our error
        // taxonomy: 5xx responses we PROXIED FROM upstream (LLM
        // provider 503s, MCP server 5xx) are downstream failures —
        // operational signals that belong at WARN, not ERROR
        // (which we reserve for our own code being wrong). The
        // dedicated `system_alerts` path already emits the correct
        // WARN with structured fields; this only governs the raw
        // tower-http "response failed" line that fires for every
        // 5xx regardless of origin. Demoting it to WARN keeps our
        // dashboards' ERROR signal honest — an Anthropic blip no
        // longer pages on-call.
        .layer(TraceLayer::new_for_http().on_failure(DefaultOnFailure::new().level(Level::WARN)))
        // The tenant maintenance gate. Added BEFORE
        // TenantLayer, which in tower's stack semantics puts it INSIDE it:
        // the gate reads `TenantContext` from the request extensions, so it
        // must run after tenant resolution has put one there. (It fails open
        // when there is none — health probes and OAuth paths skip tenant
        // resolution entirely and must never be gated.)
        //
        // Wired here rather than left to the middleware's own construction
        // because until it is in this chain, `maintenance_mode` is a column
        // the console writes and nothing reads: an operator could set
        // `paused` during an incident, see it stored, and watch every proxy
        // request keep succeeding.
        .layer(middleware::maintenance::MaintenanceLayer::new(
            state.clone(),
            state.maintenance_cache.clone(),
        ))
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        // Protocol routes open to any origin, everything else to
        // BRUTOR_CORS_ORIGINS only, never credentials. See middleware/cors.rs.
        .layer(middleware::cors::layer(&state.config.cors_origins))
        // Outermost layer (added LAST → applied FIRST per
        // axum's tower-stack semantics) so total request duration
        // spans every other middleware. Instrumentation:
        // captures `pg_stat_activity` when a request crosses the
        // per-route slow threshold.
        .layer(middleware::slow_request::SlowRequestLayer::new(
            state.diagnostic_pool.clone(),
        ))
        // Cancellation marker. Sits inside the slow-request
        // layer so the cancellation flag covers the inner stack
        // (tenant resolution, route handler, upstream call). The
        // `DispatchGuard::Drop` reads this flag via the `CANCELLED`
        // task-local to distinguish task cancellation (operational
        // signal, INFO) from a developer bug (caller didn't
        // finalize, WARN).
        .layer(middleware::cancellation_marker::CancellationMarkerLayer::new())
        // Scope the client's `X-Brutor-Run-End` request for
        // the whole inner stack, so whichever proxy route handles this step
        // stamps the marker onto its own proxy_logs row. Innermost of the
        // cross-cutting layers because it only needs to cover log building;
        // it does no work when the header is absent, which is every request
        // but the last one of a run.
        .layer(middleware::run_end::RunEndLayer::new(
            state.mcp_sessions.clone(),
        ))
        // Request-scoped memo for the effective-limits
        // chain. Must wrap the handler (that is all it needs), so it sits
        // innermost of the cross-cutting layers; it does nothing but install
        // an empty map, and every lookup outside its scope keeps querying.
        .layer(axum::middleware::from_fn(
            middleware::request_memo::scope_request_memos,
        ))
        .with_state(state);

    Ok(app)
}

/// The Prometheus metrics application. Served on its own listener
/// (`BRUTOR_METRICS_LISTEN_ADDR`), never on the public one, and only when metrics
/// are enabled.
pub fn metrics_router(state: SharedState) -> Router {
    routes::metrics::router().with_state(state)
}
