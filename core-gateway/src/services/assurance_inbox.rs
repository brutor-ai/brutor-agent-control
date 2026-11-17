// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// Assurance Inbox — core-side emitters.
//
// The control plane owns the schema and the triage API; the core only
// RAISES items for the runtime signals it alone observes: guardrail
// blocks, run terminal failures, budget hard-stops, liveness misses and
// approval timeouts. One race-safe upsert against the partial unique
// index `uq_inbox_open_fingerprint (tenant_id, fingerprint) WHERE state
// <> 'resolved'`:
//
//   * no open item  → INSERT + a `created` trail event
//   * open item     → occurrence_count++, last_seen_at, severity max
//                     (dedup — a storm is one item with a big counter)
//
// Emitters NEVER fail their caller: every function here logs and returns
// on error. A broken inbox must not break enforcement, finalization or
// sweeps.

use serde_json::Value;
use sqlx::PgPool;
use ulid::Ulid;

pub const SEV_INFO: &str = "info";
pub const SEV_WARNING: &str = "warning";
pub const SEV_CRITICAL: &str = "critical";

/// Race-safe dedup upsert. `(inserted, item_id)` on success.
///
/// Severity max uses an explicit CASE over the closed vocabulary rather
/// than string comparison ('critical' < 'info' lexically!).
const UPSERT_SQL: &str = r#"
INSERT INTO assurance_inbox_items
    (id, tenant_id, ai_system_id, category, severity, fingerprint, title,
     evidence, occurrence_count, first_seen_at, last_seen_at, state)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, NOW(), NOW(), 'open')
ON CONFLICT (tenant_id, fingerprint) WHERE state != 'resolved'
DO UPDATE SET
    occurrence_count = assurance_inbox_items.occurrence_count + 1,
    last_seen_at = NOW(),
    severity = CASE
        WHEN EXCLUDED.severity = 'critical'
             OR assurance_inbox_items.severity = 'critical' THEN 'critical'
        WHEN EXCLUDED.severity = 'warning'
             OR assurance_inbox_items.severity = 'warning' THEN 'warning'
        ELSE assurance_inbox_items.severity
    END,
    evidence = assurance_inbox_items.evidence || EXCLUDED.evidence
RETURNING id, (xmax = 0) AS inserted
"#;

/// Raise (or bump) an inbox item. Logs and swallows every error.
pub async fn raise_item(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: Option<&str>,
    category: &str,
    severity: &str,
    fingerprint: &str,
    title: &str,
    evidence: Value,
) -> Option<String> {
    let title: String = title.chars().take(500).collect();
    let row: Result<(String, bool), sqlx::Error> = sqlx::query_as(UPSERT_SQL)
        .bind(format!("inbox-{}", Ulid::new()))
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(category)
        .bind(severity)
        .bind(fingerprint)
        .bind(&title)
        .bind(&evidence)
        .fetch_one(db)
        .await;
    match row {
        Ok((item_id, inserted)) => {
            if inserted {
                // The `created` trail event. Re-occurrence events are left
                // to the occurrence counter on this side — the CP service
                // writes throttled `reoccurred` events for its own
                // emitters; the counter is the precise record either way.
                let ev: Result<_, sqlx::Error> = sqlx::query(
                    "INSERT INTO assurance_inbox_events \
                     (id, item_id, tenant_id, at, admin, event, detail) \
                     VALUES ($1, $2, $3, NOW(), NULL, 'created', NULL)",
                )
                .bind(format!("inboxev-{}", Ulid::new()))
                .bind(&item_id)
                .bind(tenant_id)
                .execute(db)
                .await;
                if let Err(e) = ev {
                    tracing::warn!(error = %e, item_id = %item_id,
                        "assurance inbox: created-event insert failed");
                }
            }
            Some(item_id)
        }
        Err(e) => {
            tracing::warn!(
                error = %e,
                tenant_id = %tenant_id,
                category = %category,
                fingerprint = %fingerprint,
                "assurance inbox: raise_item failed"
            );
            None
        }
    }
}

/// Liveness detector severities → inbox vocabulary.
pub fn liveness_severity(detector_severity: &str) -> &'static str {
    match detector_severity {
        "critical" | "high" => SEV_CRITICAL,
        "medium" => SEV_WARNING,
        _ => SEV_WARNING,
    }
}

/// Which finished runs are review findings: `errored` and `exhausted`.
///
/// A run the gateway refused is `errored` with error cause
/// `blocked_by_policy`; it is deliberately excluded, as `blocked_policy` was:
/// the guardrail/policy emitter already raised a sharper item for the block
/// itself, and a second one per run would bury it.
pub fn run_failure_severity(
    terminal_state: &str,
    error_cause: Option<&str>,
) -> Option<&'static str> {
    if error_cause == Some(crate::services::run_outcome::error_cause::BLOCKED_BY_POLICY) {
        return None;
    }
    match terminal_state {
        "errored" => Some(SEV_WARNING),
        "exhausted" => Some(SEV_WARNING),
        _ => None,
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn raise_guardrail_block(
    db: &PgPool,
    tenant_id: &str,
    resource_group_id: Option<&str>,
    guardrail_config_id: Option<&str>,
    guardrail_check: Option<&str>,
    guardrail_action: Option<&str>,
    proxy_log_id: &str,
    surface: &str,
) {
    // Warn-mode hits land at `info` (default-filtered in the UI) so a
    // pilot builds disposition history before enforcement is switched on;
    // blocks land at `warning`.
    let severity = if guardrail_action == Some("block") {
        SEV_WARNING
    } else {
        SEV_INFO
    };
    let config = guardrail_config_id.unwrap_or("unknown");
    let check = guardrail_check.unwrap_or("unknown");
    let fingerprint = format!(
        "guardrail_block:{}:{}:{}",
        resource_group_id.unwrap_or("-"),
        config,
        check,
    );
    let title = format!(
        "Guardrail {} {} on {} surface",
        check,
        if severity == SEV_WARNING {
            "blocked content"
        } else {
            "flagged content (warn mode)"
        },
        surface,
    );
    raise_item(
        db,
        tenant_id,
        resource_group_id,
        "guardrail_block",
        severity,
        &fingerprint,
        &title,
        serde_json::json!({
            "proxy_log_id": proxy_log_id,
            "guardrail_config_id": guardrail_config_id,
            "guardrail_check": guardrail_check,
            "guardrail_action": guardrail_action,
            "surface": surface,
        }),
    )
    .await;
}

pub async fn raise_run_failure(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: Option<&str>,
    run_id: &str,
    terminal_state: &str,
    error_cause: Option<&str>,
) {
    let Some(severity) = run_failure_severity(terminal_state, error_cause) else {
        return;
    };
    let fingerprint = format!(
        "run_failure:{}:{}",
        ai_system_id.unwrap_or("-"),
        terminal_state,
    );
    raise_item(
        db,
        tenant_id,
        ai_system_id,
        "run_failure",
        severity,
        &fingerprint,
        &match error_cause {
            Some(c) => format!("Run finished {terminal_state}: {}", c.replace('_', " ")),
            None => format!("Run finished {terminal_state}"),
        },
        serde_json::json!({
            "run_id": run_id,
            "terminal_state": terminal_state,
            "error_cause": error_cause,
        }),
    )
    .await;
}

pub async fn raise_budget_stop(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    group_is_ai_system: bool,
    metric: &str,
    utilization_pct: f64,
) {
    let fingerprint = format!("budget_stop:{group_id}:{metric}");
    raise_item(
        db,
        tenant_id,
        group_is_ai_system.then_some(group_id),
        "budget_stop",
        SEV_CRITICAL,
        &fingerprint,
        &format!("Budget hard-stop: {metric} at {utilization_pct:.0}% — calls are being denied"),
        serde_json::json!({
            "group_id": group_id,
            "metric": metric,
            "utilization_pct": utilization_pct,
        }),
    )
    .await;
}

pub async fn raise_liveness_miss(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    detector: &str,
    detector_severity: &str,
    title: &str,
    system_alert_id: Option<&str>,
) {
    let fingerprint = format!("liveness:{ai_system_id}:{detector}");
    raise_item(
        db,
        tenant_id,
        Some(ai_system_id),
        "liveness",
        liveness_severity(detector_severity),
        &fingerprint,
        title,
        serde_json::json!({
            "detector": detector,
            "system_alert_id": system_alert_id,
        }),
    )
    .await;
}

pub async fn raise_approval_timeout(
    db: &PgPool,
    tenant_id: &str,
    approval_request_id: &str,
    tool_name: Option<&str>,
) {
    // Fingerprint on the tool, not the request: fifty timeouts for the
    // same un-actioned tool are one finding with a big counter.
    let fingerprint = format!(
        "approval_timeout:{}",
        tool_name.unwrap_or(approval_request_id),
    );
    raise_item(
        db,
        tenant_id,
        None,
        "approval_timeout",
        SEV_INFO,
        &fingerprint,
        &format!(
            "Tool approval request expired unactioned{}",
            tool_name.map(|t| format!(": {t}")).unwrap_or_default(),
        ),
        serde_json::json!({
            "approval_request_id": approval_request_id,
            "tool_name": tool_name,
        }),
    )
    .await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_failures_map_only_errored_and_exhausted() {
        assert_eq!(
            run_failure_severity("errored", Some("model_unavailable")),
            Some(SEV_WARNING)
        );
        assert_eq!(run_failure_severity("errored", None), Some(SEV_WARNING));
        assert_eq!(
            run_failure_severity("exhausted", Some("iteration_limit")),
            Some(SEV_WARNING)
        );
        // A gateway refusal (errored / blocked_by_policy) is the guardrail
        // emitter's job; success states are not findings.
        assert_eq!(
            run_failure_severity("errored", Some("blocked_by_policy")),
            None
        );
        assert_eq!(run_failure_severity("blocked_policy", None), None);
        assert_eq!(run_failure_severity("completed", None), None);
        assert_eq!(run_failure_severity("completed_degraded", None), None);
        assert_eq!(run_failure_severity("abandoned", None), None);
        assert_eq!(run_failure_severity("cancelled", None), None);
    }

    #[test]
    fn liveness_severities_map_into_inbox_vocab() {
        assert_eq!(liveness_severity("critical"), SEV_CRITICAL);
        assert_eq!(liveness_severity("high"), SEV_CRITICAL);
        assert_eq!(liveness_severity("medium"), SEV_WARNING);
        assert_eq!(liveness_severity("low"), SEV_WARNING);
    }
}
