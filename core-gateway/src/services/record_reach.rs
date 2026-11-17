// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cross-run record reach.
//!
//! Every per-run control judges one run. Slow exfiltration is made of runs
//! that are each fine: 25 balance enquiries, each in scope for its own ticket,
//! all opened by the same requester in one afternoon.
//! The signal is how many DIFFERENT records one requester, key or agent
//! reached in a window. This sweep counts them from the record accesses of the run data scope
//! against the limits an operator declared, and raises a breach as a drift
//! event so the existing response policies can act on it.
//!
//! Detection, not admission: refusing the N+1th record inline is a usage
//! limit, which must be one atomic admission across replicas (`README.md`,
//! "Engineering rules", rule 8).

use chrono::{DateTime, Duration, Utc};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::{FromRow, PgPool};

use crate::services::drift::{
    basis, cause, detect, drift_class,
    exclusion::Evidence,
    metric::REACH_PREFIX,
    sweeper::{self, DriftCandidate, Finding, SweepOutcome},
};

/// Accesses older than this are pruned. The longest window is 30 days.
pub const RETENTION_DAYS: i64 = 35;
const PRUNE_BATCH: i64 = 10_000;
const NAMED_RUNS: usize = 5;

pub mod group_by {
    pub const SUBJECT: &str = "subject";
    pub const API_KEY: &str = "api_key";
    pub const AGENT: &str = "agent";
    pub const SYSTEM: &str = "system";
    pub const SCOPE_PREFIX: &str = "scope:";
}

/// The value a group takes when the run did not say.
pub const UNDECLARED: &str = "(undeclared)";

#[derive(Debug, Clone, FromRow)]
pub struct ReachRule {
    pub id: String,
    pub tenant_id: String,
    pub ai_system_id: String,
    pub system_name: Option<String>,
    pub system_kind: Option<String>,
    pub scope_key: String,
    pub group_by: String,
    pub window_hours: i32,
    pub max_distinct: i32,
    pub severity: String,
}

/// One group over its limit.
#[derive(Debug, Clone, FromRow)]
pub struct Breach {
    pub grp: String,
    pub distinct_records: i64,
    pub first_at: DateTime<Utc>,
    pub last_at: DateTime<Utc>,
    pub root_task_ids: Vec<String>,
}

/// The SQL expression a rule groups by. Only whitelisted shapes are produced;
/// a `scope:<key>` key is bound as a parameter, never spliced.
fn group_expr(group_by: &str) -> Option<&'static str> {
    Some(match group_by {
        group_by::SUBJECT => "COALESCE(a.subject_id, '(undeclared)')",
        group_by::API_KEY => "COALESCE(a.api_key_id, '(undeclared)')",
        group_by::AGENT => "COALESCE(a.agent_id, '(undeclared)')",
        group_by::SYSTEM => "'system'",
        g if g.starts_with(group_by::SCOPE_PREFIX) => {
            "COALESCE((SELECT string_agg(v, ',' ORDER BY v) \
                          FROM ai_system_run_scopes s, \
                               jsonb_array_elements_text(s.scope_values) v \
                         WHERE s.tenant_id = a.tenant_id \
                           AND s.root_task_id = a.root_task_id \
                           AND s.scope_key = $6), '(undeclared)')"
        }
        _ => return None,
    })
}

/// Distinct records per group in `[since, now)`, only the groups over the
/// limit. Refused attempts count: trying a record is reaching for it.
pub fn breach_sql(expr: &str) -> String {
    format!(
        "SELECT grp, COUNT(DISTINCT record_ref) AS distinct_records, \
                MIN(accessed_at) AS first_at, MAX(accessed_at) AS last_at, \
                (ARRAY_AGG(DISTINCT root_task_id))[1:{NAMED_RUNS}] AS root_task_ids \
           FROM (SELECT a.record_ref, a.accessed_at, a.root_task_id, {expr} AS grp \
                   FROM ai_system_record_access a \
                  WHERE a.tenant_id = $1 AND a.ai_system_id = $2 AND a.scope_key = $3 \
                    AND a.accessed_at >= $4 AND a.record_ref IS NOT NULL) x \
          GROUP BY grp \
         HAVING COUNT(DISTINCT record_ref) > $5 \
          ORDER BY distinct_records DESC"
    )
}

/// `reach:<rule>:<group hash>` — the group may be a person's id, and the key
/// has to fit `varchar(64)`.
pub fn metric_key(rule_id: &str, group: &str) -> String {
    let h = format!("{:x}", Sha256::digest(group.as_bytes()));
    let mut key = format!("{REACH_PREFIX}{rule_id}:{}", &h[..12]);
    key.truncate(64);
    key
}

pub async fn breaches(
    db: &PgPool,
    rule: &ReachRule,
    now: DateTime<Utc>,
) -> Result<Vec<Breach>, sqlx::Error> {
    let Some(expr) = group_expr(&rule.group_by) else {
        tracing::warn!(rule_id = %rule.id, group_by = %rule.group_by, "reach rule: unknown group_by; skipped");
        return Ok(Vec::new());
    };
    let scope_key = rule
        .group_by
        .strip_prefix(group_by::SCOPE_PREFIX)
        .unwrap_or("");
    sqlx::query_as::<_, Breach>(&breach_sql(expr))
        .bind(&rule.tenant_id)
        .bind(&rule.ai_system_id)
        .bind(&rule.scope_key)
        .bind(now - Duration::hours(i64::from(rule.window_hours)))
        .bind(i64::from(rule.max_distinct))
        .bind(scope_key)
        .fetch_all(db)
        .await
}

fn finding(rule: &ReachRule, b: &Breach) -> Finding {
    let severity = match rule.severity.as_str() {
        "critical" => detect::severity::CRITICAL,
        "medium" => detect::severity::MEDIUM,
        "low" => detect::severity::LOW,
        _ => detect::severity::HIGH,
    };
    let headline = format!(
        "{} {} reached {} distinct '{}' records in the last {} hours; the limit is {}.",
        rule.group_by,
        b.grp,
        b.distinct_records,
        rule.scope_key,
        rule.window_hours,
        rule.max_distinct
    );
    Finding {
        basis: basis::RUNS,
        caller_system_id: None,
        bucket: crate::services::drift::baseline::ALL_BUCKET.to_string(),
        detection: detect::Detection {
            metric_key: metric_key(&rule.id, &b.grp),
            drift_class: drift_class::BEHAVIOURAL,
            baseline_value: Some(f64::from(rule.max_distinct)),
            observed_value: Some(b.distinct_records as f64),
            effect_size: b.distinct_records as f64 / f64::from(rule.max_distinct.max(1)),
            direction: "up",
            severity,
            change_point_index: None,
        },
        attribution: cause::Attribution {
            cause: cause::kind::UNEXPLAINED,
            confidence: 0.0,
            evidence: json!({
                "reach_rule_id": rule.id,
                "scope_key": rule.scope_key,
                "group_by": rule.group_by,
                "group": b.grp,
                "distinct_records": b.distinct_records,
                "max_distinct": rule.max_distinct,
                "window_hours": rule.window_hours,
                "root_task_ids": b.root_task_ids,
                "first_at": b.first_at,
                "last_at": b.last_at,
            }),
            summary: "Each run may be in scope on its own; the count is across runs. \
                      Review the runs named in the evidence."
                .into(),
        },
        change_point: Some(b.first_at),
        // A human closing the finding (false positive, accepted) settles the
        // accesses seen so far: `settles` compares this with the closure, so
        // the finding returns only when the group reaches for more records.
        evidence: Evidence {
            baseline_from: Some(b.last_at),
            observed_from: Some(b.first_at),
            observed_to: Some(b.last_at),
            change_point: Some(b.first_at),
        },
        headline: Some(headline),
    }
}

const RULES_SQL: &str = "SELECT r.id, r.tenant_id, r.ai_system_id, \
        COALESCE(g.display_name, g.name) AS system_name, g.system_kind, \
        r.scope_key, r.group_by, r.window_hours, r.max_distinct, r.severity \
   FROM ai_system_reach_rules r \
   JOIN resource_groups g ON g.id = r.ai_system_id AND g.tenant_id = r.tenant_id \
  WHERE r.enabled AND g.is_active";

/// One sweep over every enabled rule. Returns (rules checked, findings).
pub async fn sweep(db: &PgPool, now: DateTime<Utc>) -> (usize, usize) {
    let rules: Vec<ReachRule> = match sqlx::query_as(RULES_SQL).fetch_all(db).await {
        Ok(r) => r,
        Err(e) => {
            crate::services::db_errors::record(&e);
            tracing::warn!(error = %e, "reach sweep: rules unreadable; skipped this tick");
            return (0, 0);
        }
    };
    let mut found = 0;
    for rule in &rules {
        let list = match breaches(db, rule, now).await {
            Ok(l) => l,
            Err(e) => {
                crate::services::db_errors::record(&e);
                tracing::warn!(error = %e, rule_id = %rule.id, "reach sweep: count failed");
                continue;
            }
        };
        let candidate = DriftCandidate {
            tenant_id: rule.tenant_id.clone(),
            ai_system_id: rule.ai_system_id.clone(),
            system_name: rule.system_name.clone(),
            system_kind: rule.system_kind.clone(),
            autonomy_level: None,
            risk_tier: None,
            eligible_runs: 0,
        };
        for b in &list {
            let mut outcome = SweepOutcome::default();
            sweeper::record(db, &candidate, &finding(rule, b), now, &mut outcome).await;
            found += outcome.alerted;
        }
    }
    prune(db, now).await;
    (rules.len(), found)
}

/// Remove accesses and run scopes past retention, in bounded batches.
pub async fn prune(db: &PgPool, now: DateTime<Utc>) {
    let cutoff = now - Duration::days(RETENTION_DAYS);
    for (table, col) in [
        ("ai_system_record_access", "accessed_at"),
        ("ai_system_run_scopes", "declared_at"),
    ] {
        let sql = format!(
            "DELETE FROM {table} WHERE ctid IN \
               (SELECT ctid FROM {table} WHERE {col} < $1 LIMIT {PRUNE_BATCH})"
        );
        if let Err(e) = sqlx::query(&sql).bind(cutoff).execute(db).await {
            crate::services::db_errors::record(&e);
            tracing::warn!(error = %e, table, "reach sweep: prune failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_groupings_produce_sql() {
        for g in ["subject", "api_key", "agent", "system", "scope:requester"] {
            assert!(group_expr(g).is_some(), "{g}");
        }
        assert!(group_expr("subject; DROP TABLE x").is_none());
        // A scope key is a bound parameter, never part of the SQL text.
        assert!(group_expr("scope:x'); DROP").unwrap().contains("$6"));
    }

    #[test]
    fn a_metric_key_fits_and_does_not_carry_the_group() {
        let k = metric_key("rrl-01JC0000000000000000000000", "user-42@example.com");
        assert!(k.len() <= 64, "{k}");
        assert!(k.starts_with("reach:rrl-"));
        assert!(!k.contains("example.com"));
        assert_ne!(k, metric_key("rrl-01JC0000000000000000000000", "user-43"));
    }
}
