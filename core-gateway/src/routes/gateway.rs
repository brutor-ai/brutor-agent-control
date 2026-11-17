// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Gateway status/health routes.

use axum::Router;
use axum::extract::State;
use axum::response::Json;
use axum::routing::get;
use serde_json::{Value, json};

use crate::services::SharedState;

/// Health check response.
#[derive(serde::Serialize, utoipa::ToSchema)]
struct HealthResponse {
    status: String,
    service: String,
    components: Value,
    stats: Value,
}

/// Version/info response.
#[derive(serde::Serialize, utoipa::ToSchema)]
struct VersionResponse {
    service: String,
    version: String,
    runtime: String,
    /// Which edition answers: `community` or `enterprise`. What the process
    /// is composed of (`services::edition::Composition`), not a build flag.
    edition: String,
    features: Value,
}

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/v1/version", get(version))
        .route("/health", get(health))
        .route("/healthz", get(health))
}

#[utoipa::path(
    get,
    path = "/v1/version",
    tag = "Gateway",
    summary = "Service version, edition and feature flags",
    responses(
        (status = 200, description = "Version info", body = VersionResponse)
    )
)]
pub async fn version(State(state): State<SharedState>) -> Json<Value> {
    Json(version_of(&state.composition))
}

/// What `GET /v1/version` answers for a composition: [`version_info`] of its
/// edition, and, for a composition whose extensions grant entitlements,
/// which it holds (`entitlements`, by name) and which extensions were left
/// out for want of one (`withheld`). A Community composition has neither
/// member.
pub fn version_of(composition: &crate::services::edition::Composition) -> Value {
    let mut info = version_info(composition.edition());
    if let Some(entitlements) = composition.entitlements() {
        info["entitlements"] = json!(entitlements);
        info["withheld"] = composition
            .withheld()
            .iter()
            .map(|(entitlement, extension)| {
                json!({"entitlement": entitlement, "extension": extension})
            })
            .collect();
    }
    info
}

/// What `GET /v1/version` answers for a process of `edition`. Open (no
/// credential, no tenant): a client, a deployment probe and the release
/// tooling read the edition here instead of inferring it from a 404.
pub fn version_info(edition: crate::services::edition::Edition) -> Value {
    json!({
        "service": "brutor-gateway-core",
        "version": env!("CARGO_PKG_VERSION"),
        "runtime": "rust/tokio",
        "edition": edition.as_str(),
        "features": {
            "llm_streaming": true,
            "mcp_proxy": true,
            "virtual_mcp": true,
            "oauth_proxy": true,
            "skill_execution": true,
            "guardrails": true,
            "usage_tracking": true,
        }
    })
}

#[utoipa::path(
    get,
    path = "/health",
    tag = "Gateway",
    summary = "Health check with component status",
    responses(
        (status = 200, description = "Health status", body = HealthResponse)
    )
)]
pub(crate) async fn health(State(state): State<SharedState>) -> Json<Value> {
    let db_ok = sqlx::query("SELECT 1").execute(&state.db).await.is_ok();

    let redis_ok = if let Some(ref _redis) = state.redis {
        true // Connection manager auto-reconnects
    } else {
        false
    };

    let bg_stats = state.background_writer.stats();

    Json(json!({
        "status": if db_ok { "healthy" } else { "degraded" },
        "service": "brutor-gateway-core",
        "components": {
            "database": if db_ok { "up" } else { "down" },
            "redis": if redis_ok { "up" } else { "not_configured" },
        },
        // `active_mcp_sessions` was removed here. Nothing ever
        // inserted into the session map, so it reported 0 on every deployment
        // forever: a number that reads as "no sessions right now" when it
        // actually meant "not measured". A health field that cannot be
        // non-zero is worse than an absent one.
        "stats": {
            "background_writes": {
                "scheduled": bg_stats.total_scheduled.load(std::sync::atomic::Ordering::Relaxed),
                "completed": bg_stats.total_completed.load(std::sync::atomic::Ordering::Relaxed),
                "failed": bg_stats.total_failed.load(std::sync::atomic::Ordering::Relaxed),
                // Rows shed under telemetry backpressure (proxy_metrics; a
                // usage_logs row only when its overflow is full too).
                // Non-zero ⇒ DB can't keep up.
                "dropped": bg_stats.total_dropped.load(std::sync::atomic::Ordering::Relaxed),
                // proxy_logs (audit) moved to the durable spool, and
                // audit rows lost past every bound (each logged at ERROR).
                "audit_spooled": bg_stats.audit_spooled.load(std::sync::atomic::Ordering::Relaxed),
                "audit_lost": bg_stats.audit_lost.load(std::sync::atomic::Ordering::Relaxed),
                // usage_logs rows moved to the durable spool, and rows
                // the database refused (kept on the dead list).
                "usage_spooled": bg_stats.total_spooled.load(std::sync::atomic::Ordering::Relaxed),
                "usage_dead_lettered": bg_stats.total_dead_lettered.load(std::sync::atomic::Ordering::Relaxed),
            }
        }
    }))
}
