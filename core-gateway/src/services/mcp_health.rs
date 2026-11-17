// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Health Updater
//!
//! Live health status updates for **both MCP servers and LLM models**
//! based on actual proxy traffic.
//!
//! Uses fire-and-forget `tokio::spawn` for DB writes so the request path
//! is never blocked.
//!
//! Write throttling: only writes to the database if the health status
//! changed OR more than 60 seconds have elapsed since the last write.
//! This prevents write storms under high traffic while still keeping
//! the health status reasonably fresh.
//!
//! Health transitions raise a `resource_health` alert via the
//! [`system_alerts`](crate::services::system_alerts) service — at most
//! one active alert per (tenant, resource_type, resource_id); re-raises
//! bump `occurrence_count` rather than inserting duplicate rows. The
//! auto-resolver background task clears the alert once the resource
//! has been healthy for 5+ minutes.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use sqlx::PgPool;

use crate::services::system_alerts::{SystemAlertInput, spawn_raise};

/// Tracks when we last wrote health status for each resource to avoid write storms.
///
/// Only writes to the database if the status changed OR more than 60 seconds
/// have elapsed since the last write. Designed to be stored in `AppState`
/// and shared across all request handlers.
pub struct McpHealthUpdater {
    last_writes: DashMap<String, Mutex<HealthWriteState>>,
}

/// Internal tracking state for a single resource's health writes.
struct HealthWriteState {
    last_status: String,
    last_write: Instant,
}

/// Minimum interval between health writes for the same resource and status.
const WRITE_INTERVAL: Duration = Duration::from_secs(60);

/// SQL templates for the two supported resource types.
/// Avoids string formatting on the hot path.
struct HealthSql {
    success: &'static str,
    failure: &'static str,
    circuit_open: &'static str,
    circuit_close: &'static str,
}

const MCP_SQL: HealthSql = HealthSql {
    success: "UPDATE mcp_servers SET health_status = 'healthy', last_health_check = NOW(), \
              last_health_success = NOW(), consecutive_health_failures = 0, health_check_error = NULL \
              WHERE id = $1 AND tenant_id = $2",
    failure: "UPDATE mcp_servers SET \
              health_status = CASE WHEN COALESCE(consecutive_health_failures, 0) >= 4 THEN 'unhealthy' ELSE 'degraded' END, \
              last_health_check = NOW(), \
              consecutive_health_failures = COALESCE(consecutive_health_failures, 0) + 1, \
              health_check_error = $3 \
              WHERE id = $1 AND tenant_id = $2",
    circuit_open: "UPDATE mcp_servers SET health_status = 'unhealthy', last_health_check = NOW(), \
                   health_check_error = 'Circuit breaker open' \
                   WHERE id = $1 AND tenant_id = $2",
    circuit_close: "UPDATE mcp_servers SET health_status = 'healthy', last_health_check = NOW(), \
                    last_health_success = NOW(), consecutive_health_failures = 0, health_check_error = NULL \
                    WHERE id = $1 AND tenant_id = $2",
};

// Restricted-tier models (OpenAI Codex preview, Pro tier, o1-pro,
// Gemini previews, …) reject every probe with the configured key.
// Without special handling, this runtime-failure path would write
// ``degraded`` (1–3 failures) or ``unhealthy`` (4+) over the
// ``requires_tier`` status that the control-plane test_connection
// sets — and the admin UI would render a yellow "Degraded" or red
// "Unhealthy" badge for what is actually an eligibility-gated model
// the operator has no way to fix without upgrading their key.
//
// The CASE branches read ``capabilities->>'restricted_tier'`` and
// short-circuit to ``requires_tier`` for those models on every
// non-success path.
const LLM_SQL: HealthSql = HealthSql {
    success: "UPDATE llm_model SET health_status = 'healthy', last_health_check = NOW(), \
              last_health_success = NOW(), consecutive_health_failures = 0, health_check_error = NULL \
              WHERE id = $1 AND tenant_id = $2",
    failure: "UPDATE llm_model SET \
              health_status = CASE \
                WHEN (capabilities->>'restricted_tier')::boolean IS TRUE THEN 'requires_tier' \
                WHEN COALESCE(consecutive_health_failures, 0) >= 4 THEN 'unhealthy' \
                ELSE 'degraded' END, \
              last_health_check = NOW(), \
              consecutive_health_failures = COALESCE(consecutive_health_failures, 0) + 1, \
              health_check_error = $3 \
              WHERE id = $1 AND tenant_id = $2",
    circuit_open: "UPDATE llm_model SET \
                   health_status = CASE \
                     WHEN (capabilities->>'restricted_tier')::boolean IS TRUE THEN 'requires_tier' \
                     ELSE 'unhealthy' END, \
                   last_health_check = NOW(), \
                   health_check_error = 'Circuit breaker open' \
                   WHERE id = $1 AND tenant_id = $2",
    circuit_close: "UPDATE llm_model SET health_status = 'healthy', last_health_check = NOW(), \
                    last_health_success = NOW(), consecutive_health_failures = 0, health_check_error = NULL \
                    WHERE id = $1 AND tenant_id = $2",
};

impl McpHealthUpdater {
    /// Create a new health updater with an empty tracking map.
    pub fn new() -> Self {
        Self {
            last_writes: DashMap::new(),
        }
    }

    // =========================================================================
    // MCP Server health (delegates to shared implementation)
    // =========================================================================

    /// Record a successful MCP proxy request.
    pub fn record_success(&self, db: PgPool, tenant_id: &str, server_id: &str) {
        self.write_success(db, tenant_id, server_id, "", &MCP_SQL);
    }

    /// Record a failed MCP proxy request.
    pub fn record_failure(&self, db: PgPool, tenant_id: &str, server_id: &str, error: &str) {
        self.write_failure(db, tenant_id, server_id, error, "mcp", &MCP_SQL);
    }

    /// Record MCP circuit breaker opening.
    pub fn record_circuit_open(&self, db: PgPool, tenant_id: &str, server_id: &str) {
        self.write_circuit_open(db, tenant_id, server_id, "mcp", &MCP_SQL);
    }

    /// Record MCP circuit breaker closing.
    pub fn record_circuit_close(&self, db: PgPool, tenant_id: &str, server_id: &str) {
        self.write_circuit_close(db, tenant_id, server_id, &MCP_SQL);
    }

    // =========================================================================
    // LLM Model health (delegates to shared implementation)
    // =========================================================================

    /// Record a successful LLM proxy request.
    pub fn record_llm_success(&self, db: PgPool, tenant_id: &str, model_id: &str) {
        self.write_success(db, tenant_id, model_id, "llm:", &LLM_SQL);
    }

    /// Record a failed LLM proxy request.
    pub fn record_llm_failure(&self, db: PgPool, tenant_id: &str, model_id: &str, error: &str) {
        self.write_failure(db, tenant_id, model_id, error, "llm", &LLM_SQL);
    }

    // =========================================================================
    // Shared implementation (DRY — MCP and LLM use the same logic)
    // =========================================================================

    fn write_success(
        &self,
        db: PgPool,
        tenant_id: &str,
        resource_id: &str,
        key_prefix: &str,
        sql: &'static HealthSql,
    ) {
        let key = format!("{key_prefix}{tenant_id}:{resource_id}");
        if !self.should_write(&key, "healthy") {
            return;
        }
        self.mark_written(&key, "healthy");

        let tid = tenant_id.to_string();
        let rid = resource_id.to_string();
        tokio::spawn(async move {
            // Health writes are local state (our own DB). If this fails it's a
            // real problem — we lose the signal that lets the auto-resolver
            // close stale alerts. Log at ERROR; the next throttle window will
            // retry.
            if let Err(e) = sqlx::query(sql.success)
                .bind(&rid)
                .bind(&tid)
                .execute(&db)
                .await
            {
                tracing::error!(
                    resource_id = %rid,
                    error = %e,
                    "Failed to write healthy status to tracking table"
                );
            }
        });
    }

    fn write_failure(
        &self,
        db: PgPool,
        tenant_id: &str,
        resource_id: &str,
        error: &str,
        resource_type: &str,
        sql: &'static HealthSql,
    ) {
        let key = format!(
            "{}{}:{}",
            if resource_type == "llm" { "llm:" } else { "" },
            tenant_id,
            resource_id,
        );

        // On the transition from healthy → degraded, raise a system alert.
        // Subsequent failures within the same degraded spell re-raise the
        // same dedup key (the helper bumps occurrence_count) but we skip
        // the extra DB call because a status flip isn't happening.
        let prev_status = self
            .last_writes
            .get(&key)
            .map(|e| e.value().lock().unwrap().last_status.clone())
            .unwrap_or_default();
        if prev_status == "healthy" || prev_status.is_empty() {
            spawn_raise(
                db.clone(),
                SystemAlertInput::resource_health_change(
                    Some(tenant_id.to_string()),
                    canonical_resource_type(resource_type),
                    resource_id,
                    None, // resource_name: we don't have the display name on the hot path
                    "degraded",
                    format!(
                        "{} {resource_id} health degraded: {error}",
                        resource_type.to_uppercase(),
                    ),
                ),
            );
        }

        self.mark_written(&key, "degraded");

        let tid = tenant_id.to_string();
        let rid = resource_id.to_string();
        let err = error.to_string();
        tokio::spawn(async move {
            if let Err(e) = sqlx::query(sql.failure)
                .bind(&rid)
                .bind(&tid)
                .bind(&err)
                .execute(&db)
                .await
            {
                tracing::error!(
                    resource_id = %rid,
                    error = %e,
                    "Failed to write failure status to tracking table"
                );
            }
        });
    }

    fn write_circuit_open(
        &self,
        db: PgPool,
        tenant_id: &str,
        resource_id: &str,
        resource_type: &str,
        sql: &'static HealthSql,
    ) {
        let key = format!(
            "{}{}:{}",
            if resource_type == "llm" { "llm:" } else { "" },
            tenant_id,
            resource_id,
        );
        self.mark_written(&key, "unhealthy");

        // Circuit-breaker trip is an immediate-attention event; always
        // raise (the dedup key keeps flapping circuits from duplicating).
        spawn_raise(
            db.clone(),
            SystemAlertInput::resource_health_change(
                Some(tenant_id.to_string()),
                canonical_resource_type(resource_type),
                resource_id,
                None,
                "unhealthy",
                format!(
                    "Circuit breaker opened for {} {resource_id} — consecutive failures exceeded threshold",
                    resource_type.to_uppercase(),
                ),
            ),
        );

        let tid = tenant_id.to_string();
        let rid = resource_id.to_string();
        tokio::spawn(async move {
            if let Err(e) = sqlx::query(sql.circuit_open)
                .bind(&rid)
                .bind(&tid)
                .execute(&db)
                .await
            {
                tracing::warn!(resource_id = %rid, error = %e, "Failed to write circuit-open status");
            }
        });
    }

    fn write_circuit_close(
        &self,
        db: PgPool,
        tenant_id: &str,
        resource_id: &str,
        sql: &'static HealthSql,
    ) {
        let key = format!("{tenant_id}:{resource_id}");
        self.mark_written(&key, "healthy");

        let tid = tenant_id.to_string();
        let rid = resource_id.to_string();
        tokio::spawn(async move {
            if let Err(e) = sqlx::query(sql.circuit_close)
                .bind(&rid)
                .bind(&tid)
                .execute(&db)
                .await
            {
                tracing::warn!(resource_id = %rid, error = %e, "Failed to write circuit-close status");
            }
        });
    }

    // =========================================================================
    // Write throttling internals
    // =========================================================================

    /// Check if we should write to the database for this key and status.
    fn should_write(&self, key: &str, new_status: &str) -> bool {
        if let Some(entry) = self.last_writes.get(key) {
            let state = entry.value().lock().unwrap();
            if state.last_status == new_status && state.last_write.elapsed() < WRITE_INTERVAL {
                return false;
            }
        }
        true
    }

    /// Update the last write tracking for a given key and status.
    fn mark_written(&self, key: &str, status: &str) {
        let entry = self.last_writes.entry(key.to_string()).or_insert_with(|| {
            Mutex::new(HealthWriteState {
                last_status: String::new(),
                last_write: Instant::now() - WRITE_INTERVAL,
            })
        });
        let mut state = entry.value().lock().unwrap();
        state.last_status = status.to_string();
        state.last_write = Instant::now();
    }
}

/// Translate the hot-path shorthand (`"mcp"` / `"llm"`) into the canonical
/// `resource_type` value used throughout `system_alerts` and the auto-resolver
/// (`"mcp_server"` / `"llm_model"`). The shorthand exists for historical
/// reasons in the health-write APIs; keep it isolated here rather than letting
/// it leak into alert dedup keys and resource-lookup queries.
fn canonical_resource_type(hot_path_name: &str) -> &'static str {
    match hot_path_name {
        "llm" => "llm_model",
        "mcp" => "mcp_server",
        // Defensive default: if a new caller shows up with something else,
        // fall through and let the downstream code handle it. The alert
        // will still be visible; only the resource-health auto-resolver
        // might fail to match — which is strictly better than panicking.
        other => match other {
            "llm_model" | "mcp_server" => other_as_static(other),
            _ => "unknown",
        },
    }
}

// Narrow shim: `&str → &'static str` for the two known canonical names so the
// match above can return a `&'static str` without allocation. Any other input
// has already been filtered to the `"unknown"` branch.
fn other_as_static(s: &str) -> &'static str {
    match s {
        "llm_model" => "llm_model",
        "mcp_server" => "mcp_server",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_resource_type_maps_hot_path_names() {
        assert_eq!(canonical_resource_type("llm"), "llm_model");
        assert_eq!(canonical_resource_type("mcp"), "mcp_server");
    }

    #[test]
    fn canonical_resource_type_passes_through_canonical_names() {
        assert_eq!(canonical_resource_type("llm_model"), "llm_model");
        assert_eq!(canonical_resource_type("mcp_server"), "mcp_server");
    }

    #[test]
    fn canonical_resource_type_falls_back_for_unknown() {
        assert_eq!(canonical_resource_type("bogus"), "unknown");
    }
}
