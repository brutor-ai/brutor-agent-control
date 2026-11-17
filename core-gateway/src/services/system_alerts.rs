// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # System Alerts
//!
//! First-class alerting for failures the on-call engineer needs to
//! know about. Distinct from [`usage_alerts`] (quota-scoped) and from
//! MCP health ticks (ephemeral status).
//!
//! ## When to raise an alert
//!
//! | Scenario                                         | Category                  | Severity |
//! |--------------------------------------------------|---------------------------|----------|
//! | OpenAI / Anthropic / Bedrock returns 5xx         | `downstream_provider`     | high     |
//! | OAuth IdP unreachable / token refresh fails      | `downstream_idp`          | high     |
//! | Redis / semantic cache unavailable               | `downstream_cache`        | medium   |
//! | Qdrant vector DB unavailable                     | `downstream_vector_db`    | medium   |
//! | LLM model or MCP server health flips             | `resource_health`         | high     |
//! | Error rate / CPU / memory threshold crossed      | `infrastructure_threshold`| medium   |
//! | Unwrap / schema drift / invariant violation      | `internal_error`          | critical |
//!
//! ## Dedup semantics
//!
//! Every alert has a deterministic `dedup_key`:
//!
//! ```text
//! {tenant_id_or_"global"}|{category}|{source}|{error_code_or_"none"}
//! ```
//!
//! A partial unique index on the DB side enforces at most one row per dedup key
//! while status is `active` or `acknowledged`. A re-raise UPSERTs:
//! bumps `last_seen_at` + `occurrence_count` instead of inserting
//! a new row. This matches how industry systems (Alertmanager,
//! PagerDuty, Opsgenie) group alert instances rather than fire
//! one row per occurrence.
//!
//! ## Observability
//!
//! [`raise`] emits, in a single place:
//!
//! 1. `tracing::warn!` (downstream/health/infra categories) or
//!    `tracing::error!` (internal_error) with structured fields
//!    (`alert_category`, `alert_source`, `alert_severity`, `tenant_id`,
//!    `error_code`, `resource_id`). The existing OTel layer
//!    (`services/telemetry.rs`) exports these as OTel events.
//! 2. Prometheus counter (`proxy_system_alerts_raised_total` on
//!    first occurrence, `proxy_system_alerts_deduped_total` on
//!    re-raise).
//!
//! Call sites never need to remember to add tracing or metrics.
//!
//! ## Fire-and-forget pattern
//!
//! Request paths MUST NOT block on alert persistence. Use
//! [`spawn_raise`] — it clones the pool, dispatches on a Tokio task,
//! and ignores the outcome. The DB write errors are caught and
//! logged inside the spawned task.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;
use std::fmt;

use crate::services::proxy_metrics;

// ---------------------------------------------------------------------------
// Enums — keep in sync with:
//   * the table's CHECK constraints
//   * Python model SystemAlert.CATEGORIES / SEVERITIES
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    Critical,
    High,
    Medium,
    Low,
}

impl Severity {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Critical => "critical",
            Self::High => "high",
            Self::Medium => "medium",
            Self::Low => "low",
        }
    }
}

impl fmt::Display for Severity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertCategory {
    DownstreamProvider,
    DownstreamIdp,
    DownstreamCache,
    DownstreamVectorDb,
    ResourceHealth,
    InfrastructureThreshold,
    InternalError,
    /// A governed AI System went silent, stalled,
    /// collapsed in volume, or started failing outright.
    ///
    /// The first category about a *workload* rather than the plumbing
    /// under it. Every variant above asks "is the infrastructure up";
    /// this one asks "is the AI System still doing anything", which
    /// nothing else can ask because every other signal is a function of
    /// traffic that arrived.
    AiSystemLiveness,
    /// The system's behaviour moved off its baseline.
    AiSystemBehaviour,
    /// The same work started costing more.
    AiSystemCost,
    /// A tool or model underneath the system changed.
    AiSystemDependency,
}

impl AlertCategory {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::DownstreamProvider => "downstream_provider",
            Self::DownstreamIdp => "downstream_idp",
            Self::DownstreamCache => "downstream_cache",
            Self::DownstreamVectorDb => "downstream_vector_db",
            Self::ResourceHealth => "resource_health",
            Self::InfrastructureThreshold => "infrastructure_threshold",
            Self::InternalError => "internal_error",
            Self::AiSystemLiveness => "ai_system_liveness",
            Self::AiSystemBehaviour => "ai_system_behaviour",
            Self::AiSystemCost => "ai_system_cost",
            Self::AiSystemDependency => "ai_system_dependency",
        }
    }

    /// Whether this category represents a failure in a service we
    /// depend on (as opposed to our own code).
    pub fn is_downstream(&self) -> bool {
        matches!(
            self,
            Self::DownstreamProvider
                | Self::DownstreamIdp
                | Self::DownstreamCache
                | Self::DownstreamVectorDb
        )
    }
}

impl fmt::Display for AlertCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// Input payload + typed constructors
// ---------------------------------------------------------------------------

/// Everything needed to raise one alert. Typed constructors below
/// (`downstream_provider`, `resource_health_change`, etc.) are the
/// preferred call-site API — they enforce consistent `source` /
/// `severity` choices across the codebase.
#[derive(Debug, Clone)]
pub struct SystemAlertInput {
    pub tenant_id: Option<String>,
    pub severity: Severity,
    pub category: AlertCategory,
    pub source: String,
    pub resource_type: Option<String>,
    pub resource_id: Option<String>,
    pub resource_name: Option<String>,
    pub error_code: Option<String>,
    pub title: String,
    pub message: String,
    pub context: Option<serde_json::Value>,
}

impl SystemAlertInput {
    /// Downstream LLM/API provider failure (5xx, timeout, connect refused).
    /// `provider` should be the short name — `"openai"`, `"anthropic"`,
    /// `"bedrock"` — so the dedup key groups by provider, not by model.
    pub fn downstream_provider(
        tenant_id: impl Into<Option<String>>,
        provider: &str,
        model_id: impl Into<Option<String>>,
        model_name: impl Into<Option<String>>,
        error_code: impl Into<Option<String>>,
        message: impl Into<String>,
    ) -> Self {
        let model_id = model_id.into();
        let model_name = model_name.into();
        let ec = error_code.into();
        let msg = message.into();
        Self {
            tenant_id: tenant_id.into(),
            severity: Severity::High,
            category: AlertCategory::DownstreamProvider,
            source: format!("llm.{provider}"),
            resource_type: Some("llm_model".to_string()),
            resource_id: model_id,
            resource_name: model_name,
            error_code: ec.clone(),
            title: format!("LLM provider {provider} failing"),
            message: msg,
            context: None,
        }
    }

    /// OAuth IdP unavailable / token exchange failure.
    pub fn downstream_idp(
        tenant_id: impl Into<Option<String>>,
        idp_id: &str,
        idp_name: impl Into<Option<String>>,
        error_code: impl Into<Option<String>>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            severity: Severity::High,
            category: AlertCategory::DownstreamIdp,
            source: format!("idp.{idp_id}"),
            resource_type: Some("oauth_idp".to_string()),
            resource_id: Some(idp_id.to_string()),
            resource_name: idp_name.into(),
            error_code: error_code.into(),
            title: "OAuth identity provider unavailable".to_string(),
            message: message.into(),
            context: None,
        }
    }

    /// Cache service outage (Redis, semantic cache). Platform-wide by default
    /// (tenant_id=None) since it affects every tenant at once.
    pub fn downstream_cache(
        service: &str, // "redis", "semantic_cache"
        error_code: impl Into<Option<String>>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: None,
            severity: Severity::Medium,
            category: AlertCategory::DownstreamCache,
            source: format!("cache.{service}"),
            resource_type: Some("cache_service".to_string()),
            resource_id: Some(service.to_string()),
            resource_name: Some(service.to_string()),
            error_code: error_code.into(),
            title: format!("Cache service {service} unavailable"),
            message: message.into(),
            context: None,
        }
    }

    /// Vector DB (Qdrant) failure. Platform-wide by default.
    pub fn downstream_vector_db(
        service: &str, // "qdrant"
        error_code: impl Into<Option<String>>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: None,
            severity: Severity::Medium,
            category: AlertCategory::DownstreamVectorDb,
            source: format!("vector_db.{service}"),
            resource_type: Some("vector_db".to_string()),
            resource_id: Some(service.to_string()),
            resource_name: Some(service.to_string()),
            error_code: error_code.into(),
            title: format!("Vector DB {service} unavailable"),
            message: message.into(),
            context: None,
        }
    }

    /// Health transition — an LLM model or MCP server flipped to unhealthy.
    /// Replaces the ad-hoc `create_health_alert` in `mcp_health.rs`.
    pub fn resource_health_change(
        tenant_id: impl Into<Option<String>>,
        resource_type: &str, // "llm_model" | "mcp_server"
        resource_id: &str,
        resource_name: impl Into<Option<String>>,
        current_status: &str, // "unhealthy" | "degraded" | "unknown"
        message: impl Into<String>,
    ) -> Self {
        let severity = match current_status {
            "unhealthy" => Severity::High,
            "degraded" => Severity::Medium,
            _ => Severity::Low,
        };
        Self {
            tenant_id: tenant_id.into(),
            severity,
            category: AlertCategory::ResourceHealth,
            source: format!("{resource_type}.{resource_id}"),
            resource_type: Some(resource_type.to_string()),
            resource_id: Some(resource_id.to_string()),
            resource_name: resource_name.into(),
            error_code: Some(current_status.to_string()),
            title: format!("{resource_type} unhealthy"),
            message: message.into(),
            context: None,
        }
    }

    /// Infrastructure threshold crossing (CPU, memory, error rate).
    pub fn infrastructure_threshold(
        monitor: &str, // "cpu" | "memory" | "error_rate"
        current_value: f64,
        threshold: f64,
        message: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: None,
            severity: Severity::Medium,
            category: AlertCategory::InfrastructureThreshold,
            source: format!("monitor.{monitor}"),
            resource_type: Some("system".to_string()),
            resource_id: None,
            resource_name: None,
            error_code: Some(monitor.to_string()),
            title: format!("{monitor} threshold exceeded"),
            message: message.into(),
            context: Some(serde_json::json!({
                "current_value": current_value,
                "threshold": threshold,
            })),
        }
    }

    /// Local code error — schema drift, unwrap, invariant violation.
    /// These are **always critical** because they indicate our own code
    /// is wrong, not a transient downstream issue.
    pub fn internal_error(
        tenant_id: impl Into<Option<String>>,
        source: impl Into<String>,
        error_code: impl Into<Option<String>>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            severity: Severity::Critical,
            category: AlertCategory::InternalError,
            source: source.into(),
            resource_type: None,
            resource_id: None,
            resource_name: None,
            error_code: error_code.into(),
            title: "Internal error".to_string(),
            message: message.into(),
            context: None,
        }
    }

    /// Attach structured context (correlation_id, request_id, response snippet).
    pub fn with_context(mut self, context: serde_json::Value) -> Self {
        self.context = Some(context);
        self
    }

    /// Override the default title.
    pub fn with_title(mut self, title: impl Into<String>) -> Self {
        self.title = title.into();
        self
    }

    /// Override severity. Use sparingly — most categories have sensible defaults.
    pub fn with_severity(mut self, sev: Severity) -> Self {
        self.severity = sev;
        self
    }
}

// ---------------------------------------------------------------------------
// Dedup key — MUST match the Python-side `SystemAlert.compute_dedup_key`.
// ---------------------------------------------------------------------------

/// Compose the dedup key. Deterministic, no hashing — keeps debug
/// queries readable (`SELECT * FROM system_alerts WHERE dedup_key LIKE '...|downstream_provider|%';`).
pub fn compute_dedup_key(
    tenant_id: Option<&str>,
    category: AlertCategory,
    source: &str,
    error_code: Option<&str>,
) -> String {
    format!(
        "{}|{}|{}|{}",
        tenant_id.unwrap_or("global"),
        category.as_str(),
        source,
        error_code.unwrap_or("none"),
    )
}

// ---------------------------------------------------------------------------
// Raise — the only public entry point for persisting an alert.
// ---------------------------------------------------------------------------

/// Outcome of a `raise` call. Callers rarely inspect this — the main
/// use cases are tests and future rate-limiting logic.
#[derive(Debug, Clone)]
pub struct RaiseOutcome {
    pub alert_id: String,
    /// True if this is the first occurrence (INSERT); false if the
    /// dedup key already had an active/acknowledged row and we
    /// bumped the counter (UPDATE).
    pub was_new: bool,
    pub occurrence_count: i32,
    pub last_seen_at: DateTime<Utc>,
}

/// Persist an alert, applying dedup + observability emission.
///
/// On first occurrence: INSERT new row with `occurrence_count = 1`.
/// On subsequent occurrence of the same dedup_key with an active or
/// acknowledged row: UPDATE, bumping `last_seen_at` and
/// `occurrence_count`. Acknowledged rows stay acknowledged — the
/// operator is presumed to already be aware.
///
/// Call this directly only if the caller is itself a background
/// task. From a request path, prefer [`spawn_raise`] — it won't
/// block on the DB write.
pub async fn raise(db: &PgPool, input: SystemAlertInput) -> Result<RaiseOutcome, sqlx::Error> {
    let dedup_key = compute_dedup_key(
        input.tenant_id.as_deref(),
        input.category,
        &input.source,
        input.error_code.as_deref(),
    );

    let new_id = format!("sysalert-{}", ulid::Ulid::new());

    // Partial unique index `uq_system_alerts_active_dedup` covers
    // rows WHERE status IN ('active','acknowledged'). The UPSERT
    // uses index inference — the WHERE clause on ON CONFLICT must
    // match the index predicate exactly. Resolved rows don't
    // conflict, so a new occurrence after resolution correctly
    // inserts a fresh active row.
    //
    // Returning `(xmax = 0)` gives us a boolean that's true for an
    // INSERT and false for an UPDATE. Postgres sets xmax=0 on the
    // tuple produced by an INSERT.
    let row: (
        String,        // id
        DateTime<Utc>, // last_seen_at
        i32,           // occurrence_count
        bool,          // was_insert
    ) = sqlx::query_as(
        r#"
        INSERT INTO system_alerts (
            id, tenant_id, severity, category, source,
            resource_type, resource_id, resource_name,
            error_code, title, message, context,
            status, first_seen_at, last_seen_at,
            occurrence_count, dedup_key
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8,
            $9, $10, $11, $12,
            'active', NOW(), NOW(),
            1, $13
        )
        ON CONFLICT (dedup_key) WHERE status IN ('active','acknowledged')
        DO UPDATE SET
            last_seen_at     = NOW(),
            occurrence_count = system_alerts.occurrence_count + 1,
            -- Refresh the user-facing fields with the latest info;
            -- the title/message may have improved between occurrences
            -- (e.g. provider's own error text got more specific).
            title            = EXCLUDED.title,
            message          = EXCLUDED.message,
            context          = EXCLUDED.context,
            severity         = EXCLUDED.severity
        RETURNING
            id,
            last_seen_at,
            occurrence_count,
            (xmax = 0) AS was_insert
        "#,
    )
    .bind(&new_id)
    .bind(&input.tenant_id)
    .bind(input.severity.as_str())
    .bind(input.category.as_str())
    .bind(&input.source)
    .bind(&input.resource_type)
    .bind(&input.resource_id)
    .bind(&input.resource_name)
    .bind(&input.error_code)
    .bind(&input.title)
    .bind(&input.message)
    .bind(&input.context)
    .bind(&dedup_key)
    .fetch_one(db)
    .await?;

    let (alert_id, last_seen_at, occurrence_count, was_new) = row;

    emit_observability(&input, &dedup_key, was_new, occurrence_count);

    Ok(RaiseOutcome {
        alert_id,
        was_new,
        occurrence_count,
        last_seen_at,
    })
}

/// Fire-and-forget wrapper for request paths. Never blocks, never
/// propagates errors — DB failures are logged inside the task.
pub fn spawn_raise(db: PgPool, input: SystemAlertInput) {
    tokio::spawn(async move {
        let category = input.category;
        let source = input.source.clone();
        match raise(&db, input).await {
            Ok(_) => {}
            Err(e) => {
                // Alert persistence itself failed — this is a local
                // problem. Log at ERROR so an operator notices, but
                // don't recurse into raise() (infinite loop risk).
                tracing::error!(
                    error = %e,
                    alert_category = %category,
                    alert_source = %source,
                    "Failed to persist system alert"
                );
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Observability — tracing + metrics, in one place.
// ---------------------------------------------------------------------------

fn emit_observability(
    input: &SystemAlertInput,
    dedup_key: &str,
    was_new: bool,
    occurrence_count: i32,
) {
    let resource_type = input.resource_type.as_deref().unwrap_or("unknown");

    // Prometheus counters. Separate raised vs deduped makes alert-storm
    // detection straightforward in Grafana: a sudden spike in the
    // `deduped` series relative to `raised` means the same downstream
    // is failing repeatedly.
    if was_new {
        proxy_metrics::record_system_alert_raised(
            input.category.as_str(),
            input.severity.as_str(),
            resource_type,
        );
    } else {
        proxy_metrics::record_system_alert_deduped(
            input.category.as_str(),
            input.severity.as_str(),
            resource_type,
        );
    }

    // Tracing. The OTel layer in services::telemetry picks this up and
    // exports to OTLP; logs ship to Loki through the existing stack.
    //
    // Level choice:
    //   * internal_error    → ERROR (our code has a bug)
    //   * everything else   → WARN  (something downstream is misbehaving)
    //
    // Volume control: on re-raise we emit at DEBUG instead of WARN so
    // an alert storm doesn't flood the logs with identical lines.
    // The occurrence_count field is indexed in Grafana/Loki for counting.
    let level = if input.category == AlertCategory::InternalError {
        tracing::Level::ERROR
    } else if was_new {
        tracing::Level::WARN
    } else {
        tracing::Level::DEBUG
    };

    // tracing macros don't accept a runtime Level value, so match-and-dispatch.
    // No trailing semicolons inside the macro body — we want each arm to
    // evaluate to an expression (the `()` returned by the tracing macro).
    macro_rules! emit {
        ($macro:ident) => {
            tracing::$macro!(
                alert_category = %input.category,
                alert_severity = %input.severity,
                alert_source = %input.source,
                tenant_id = ?input.tenant_id,
                resource_type = ?input.resource_type,
                resource_id = ?input.resource_id,
                error_code = ?input.error_code,
                dedup_key = %dedup_key,
                occurrence_count,
                was_new,
                "{}",
                input.title,
            )
        };
    }
    match level {
        tracing::Level::ERROR => emit!(error),
        tracing::Level::WARN => emit!(warn),
        tracing::Level::DEBUG => emit!(debug),
        _ => emit!(info),
    }
}

// ---------------------------------------------------------------------------
// Unit tests (no DB)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn severity_display_matches_db_check() {
        // These strings are compared verbatim against the DB CHECK
        // constraint on the table. Keep them stable.
        assert_eq!(Severity::Critical.as_str(), "critical");
        assert_eq!(Severity::High.as_str(), "high");
        assert_eq!(Severity::Medium.as_str(), "medium");
        assert_eq!(Severity::Low.as_str(), "low");
    }

    #[test]
    fn category_display_matches_db_check() {
        assert_eq!(
            AlertCategory::DownstreamProvider.as_str(),
            "downstream_provider"
        );
        assert_eq!(AlertCategory::DownstreamIdp.as_str(), "downstream_idp");
        assert_eq!(AlertCategory::DownstreamCache.as_str(), "downstream_cache");
        assert_eq!(
            AlertCategory::DownstreamVectorDb.as_str(),
            "downstream_vector_db"
        );
        assert_eq!(AlertCategory::ResourceHealth.as_str(), "resource_health");
        assert_eq!(
            AlertCategory::InfrastructureThreshold.as_str(),
            "infrastructure_threshold"
        );
        assert_eq!(AlertCategory::InternalError.as_str(), "internal_error");
    }

    #[test]
    fn is_downstream_correctness() {
        assert!(AlertCategory::DownstreamProvider.is_downstream());
        assert!(AlertCategory::DownstreamIdp.is_downstream());
        assert!(AlertCategory::DownstreamCache.is_downstream());
        assert!(AlertCategory::DownstreamVectorDb.is_downstream());
        assert!(!AlertCategory::ResourceHealth.is_downstream());
        assert!(!AlertCategory::InfrastructureThreshold.is_downstream());
        assert!(!AlertCategory::InternalError.is_downstream());
    }

    #[test]
    fn dedup_key_is_deterministic_and_readable() {
        let k = compute_dedup_key(
            Some("tenant-01"),
            AlertCategory::DownstreamProvider,
            "llm.openai",
            Some("503"),
        );
        assert_eq!(k, "tenant-01|downstream_provider|llm.openai|503");
    }

    #[test]
    fn dedup_key_null_tenant_becomes_global() {
        let k = compute_dedup_key(
            None,
            AlertCategory::DownstreamCache,
            "cache.redis",
            Some("connect_refused"),
        );
        assert_eq!(k, "global|downstream_cache|cache.redis|connect_refused");
    }

    #[test]
    fn dedup_key_null_error_code_becomes_none() {
        let k = compute_dedup_key(
            Some("tenant-01"),
            AlertCategory::ResourceHealth,
            "mcp.server.srv-abc",
            None,
        );
        assert_eq!(k, "tenant-01|resource_health|mcp.server.srv-abc|none");
    }

    #[test]
    fn dedup_key_matches_python_implementation() {
        // Mirror test: must produce the exact same string as the
        // Python helper in app/database/models/system_alert.py.
        // If this breaks, the two implementations have diverged
        // and re-raises will create duplicate rows.
        let cases = vec![
            (
                (None, AlertCategory::DownstreamCache, "cache.redis", None),
                "global|downstream_cache|cache.redis|none",
            ),
            (
                (
                    Some("t1"),
                    AlertCategory::DownstreamProvider,
                    "llm.anthropic",
                    Some("timeout"),
                ),
                "t1|downstream_provider|llm.anthropic|timeout",
            ),
            (
                (
                    Some("tenant-abc"),
                    AlertCategory::InternalError,
                    "portal_kb.fetch_group_collections",
                    Some("schema_drift"),
                ),
                "tenant-abc|internal_error|portal_kb.fetch_group_collections|schema_drift",
            ),
        ];
        for ((tid, cat, src, ec), expected) in cases {
            assert_eq!(compute_dedup_key(tid, cat, src, ec), expected);
        }
    }

    #[test]
    fn downstream_provider_constructor_defaults() {
        let a = SystemAlertInput::downstream_provider(
            Some("t1".to_string()),
            "openai",
            Some("model-01".to_string()),
            Some("gpt-4".to_string()),
            Some("503".to_string()),
            "Service Unavailable",
        );
        assert_eq!(a.severity, Severity::High);
        assert_eq!(a.category, AlertCategory::DownstreamProvider);
        assert_eq!(a.source, "llm.openai");
        assert_eq!(a.resource_type.as_deref(), Some("llm_model"));
        assert_eq!(a.resource_id.as_deref(), Some("model-01"));
        assert_eq!(a.error_code.as_deref(), Some("503"));
    }

    #[test]
    fn internal_error_is_always_critical() {
        let a = SystemAlertInput::internal_error(
            Some("t1".to_string()),
            "portal_kb.fetch_group_collections",
            Some("schema_drift".to_string()),
            "column kc.embedding_model_id does not exist",
        );
        assert_eq!(a.severity, Severity::Critical);
        assert_eq!(a.category, AlertCategory::InternalError);
        assert_eq!(a.source, "portal_kb.fetch_group_collections");
    }

    #[test]
    fn resource_health_severity_tracks_status() {
        let unhealthy = SystemAlertInput::resource_health_change(
            Some("t1".to_string()),
            "llm_model",
            "m-01",
            Some("GPT-4".to_string()),
            "unhealthy",
            "Last 3 health checks failed",
        );
        assert_eq!(unhealthy.severity, Severity::High);

        let degraded = SystemAlertInput::resource_health_change(
            Some("t1".to_string()),
            "mcp_server",
            "srv-01",
            Some("Weather MCP".to_string()),
            "degraded",
            "Latency p95 > 5s",
        );
        assert_eq!(degraded.severity, Severity::Medium);

        let unknown = SystemAlertInput::resource_health_change(
            Some("t1".to_string()),
            "mcp_server",
            "srv-02",
            None,
            "unknown",
            "No health check in 10 minutes",
        );
        assert_eq!(unknown.severity, Severity::Low);
    }

    #[test]
    fn infrastructure_threshold_captures_context() {
        let a = SystemAlertInput::infrastructure_threshold(
            "cpu",
            85.4,
            80.0,
            "CPU at 85% for 5 consecutive minutes",
        );
        let ctx = a.context.expect("context");
        assert_eq!(ctx["current_value"], 85.4);
        assert_eq!(ctx["threshold"], 80.0);
        assert!(a.tenant_id.is_none());
    }

    #[test]
    fn builder_with_methods() {
        let a = SystemAlertInput::downstream_cache("redis", Some("timeout".to_string()), "hung")
            .with_severity(Severity::Critical)
            .with_title("Redis catastrophically wedged")
            .with_context(serde_json::json!({ "queue_depth": 12000 }));

        assert_eq!(a.severity, Severity::Critical);
        assert_eq!(a.title, "Redis catastrophically wedged");
        assert_eq!(a.context.unwrap()["queue_depth"], 12000);
    }
}
