// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Argument-policy cache.
//!
//! Same shape as `GuardrailsCache` — TTL-evicting `(tenant, surface)`
//! -> `Vec<ArgumentPolicy>` map. The `ResourceCache` impl plugs into
//! the existing pub/sub invalidator so admin writes propagate to all
//! gateway-core pods without restart.
//!
//! Cache key is keyed by *both* tenant and group_id when the caller
//! wants the policies that apply to a specific resource group; the
//! `(tenant, __all__)` form is used by the YAML export endpoint and
//! testing.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use async_trait::async_trait;
use sqlx::PgPool;
use tokio::sync::RwLock;

use super::policy::{ArgumentPolicy, Severity, Surface};
use crate::services::cache::ResourceCache;

#[derive(Clone)]
pub struct ArgumentPolicyCache {
    inner: Arc<RwLock<HashMap<String, Entry>>>,
    ttl_secs: u64,
    // hit/miss counters reported to the operator
    // console. See `CacheStatsSnapshot` for the semantics; Arc-wrapped
    // so Clone shares the same counter state (the cache type is Clone
    // and request handlers hold their own clones).
    hits: Arc<std::sync::atomic::AtomicU64>,
    misses: Arc<std::sync::atomic::AtomicU64>,
}

pub(crate) struct Entry {
    pub(crate) policies: Vec<ArgumentPolicy>,
    pub(crate) loaded_at: Instant,
}

impl ArgumentPolicyCache {
    pub fn new(ttl_secs: u64) -> Self {
        Self {
            inner: Arc::new(RwLock::new(HashMap::new())),
            ttl_secs,
            hits: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            misses: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        }
    }

    /// Snapshot for the cache stats fan-out.
    pub fn stats(&self) -> crate::services::cache::CacheStatsSnapshot {
        // entry_count requires an async read lock; `try_read` is the
        // non-blocking equivalent that returns the current size when
        // the lock is free. Under contention we return 0 — better
        // than blocking the stats responder behind a hot writer.
        let entries = match self.inner.try_read() {
            Ok(g) => g.len() as u64,
            Err(_) => 0,
        };
        crate::services::cache::CacheStatsSnapshot {
            hits: self.hits.load(std::sync::atomic::Ordering::Relaxed),
            misses: self.misses.load(std::sync::atomic::Ordering::Relaxed),
            entries,
        }
    }

    /// Return all argument policies bound to the given resource group
    /// for the given surface. Disabled policies are returned (the
    /// runtime evaluator decides whether to fire), so an operator
    /// toggling a policy off still gets cache propagation.
    pub async fn get_for_group(
        &self,
        tenant_id: &str,
        group_id: &str,
        surface: Surface,
        db: &PgPool,
    ) -> Result<Vec<ArgumentPolicy>, sqlx::Error> {
        let key = format!("{tenant_id}:{group_id}:{}", surface_str(surface));
        {
            let guard = self.inner.read().await;
            if let Some(entry) = guard.get(&key) {
                if entry.loaded_at.elapsed().as_secs() < self.ttl_secs {
                    self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    return Ok(entry.policies.clone());
                }
            }
        }
        // Cache miss (or TTL-expired entry) — we fall through to DB.
        self.misses
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);

        let policies = self
            .load_for_group(tenant_id, group_id, surface, db)
            .await?;

        let mut guard = self.inner.write().await;
        guard.insert(
            key,
            Entry {
                policies: policies.clone(),
                loaded_at: Instant::now(),
            },
        );
        Ok(policies)
    }

    /// Load policies directly bound to the group OR any ancestor of
    /// the group (additive — same model used by guardrails). NULL-tenant
    /// is treated as "global" but currently not seeded; the query
    /// reserves space for that future extension.
    async fn load_for_group(
        &self,
        tenant_id: &str,
        group_id: &str,
        surface: Surface,
        db: &PgPool,
    ) -> Result<Vec<ArgumentPolicy>, sqlx::Error> {
        // Walk the ancestor chain via hierarchy_path. Policies bound to
        // any ancestor flow down — matches the platform's "governance
        // inherits downward" rule used by guardrails and skill/model
        // bindings. A self-binding satisfies `group_id = any_ancestor`
        // trivially because the group's own id is in its hierarchy_path.
        let surface_label = surface_str(surface);

        // hierarchy_path is `/root/child/leaf/`-style; ancestor groups
        // are those whose id appears anywhere in the leaf's hierarchy_path.
        // We collect ancestor ids via a sub-select and then look up
        // policies bound to any of them.
        let rows: Vec<PolicyRow> = sqlx::query_as::<_, PolicyRow>(
            r#"
            WITH leaf AS (
                SELECT id, hierarchy_path
                FROM resource_groups
                WHERE id = $1 AND tenant_id = $2 AND is_active = true
                LIMIT 1
            ),
            ancestor_ids AS (
                SELECT rg.id
                FROM resource_groups rg, leaf
                WHERE rg.tenant_id = $2
                  AND rg.is_active = true
                  AND (
                      leaf.hierarchy_path LIKE '%/' || rg.id || '/%'
                      OR rg.id = leaf.id
                  )
            )
            SELECT ap.id, ap.tenant_id, ap.name, ap.surface, ap.target,
                   ap.argument_key, ap.analyzer, ap.analyzer_config,
                   ap.rules, ap.enabled, ap.severity
            FROM argument_policies ap
            JOIN resource_group_argument_policies rgap
              ON rgap.argument_policy_id = ap.id
            WHERE rgap.resource_group_id IN (SELECT id FROM ancestor_ids)
              AND ap.tenant_id = $2
              AND ap.surface = $3
            "#,
        )
        .bind(group_id)
        .bind(tenant_id)
        .bind(surface_label)
        .fetch_all(db)
        .await?;

        rows.into_iter().map(PolicyRow::into_policy).collect()
    }

    /// Force-load every policy in the tenant. Used by the YAML
    /// exporter (admin path), not the hot path.
    pub async fn list_all(
        &self,
        tenant_id: &str,
        db: &PgPool,
    ) -> Result<Vec<ArgumentPolicy>, sqlx::Error> {
        let rows: Vec<PolicyRow> = sqlx::query_as::<_, PolicyRow>(
            r#"
            SELECT id, tenant_id, name, surface, target,
                   argument_key, analyzer, analyzer_config,
                   rules, enabled, severity
            FROM argument_policies
            WHERE tenant_id = $1
            ORDER BY name ASC
            "#,
        )
        .bind(tenant_id)
        .fetch_all(db)
        .await?;
        rows.into_iter().map(PolicyRow::into_policy).collect()
    }
}

#[async_trait]
impl ResourceCache for ArgumentPolicyCache {
    fn name(&self) -> &str {
        "argument_policy"
    }

    async fn invalidate(&self, tenant_id: &str, _resource_id: &str) {
        // We don't track per-policy keys (the cache is per
        // tenant+group+surface), so any policy change requires
        // dropping every entry for the tenant. This is the same
        // strategy guardrails use, and the cost is one cold-load
        // per request after invalidation.
        self.invalidate_tenant(tenant_id).await;
    }

    async fn invalidate_tenant(&self, tenant_id: &str) {
        let mut guard = self.inner.write().await;
        guard.retain(|k, _| !k.starts_with(&format!("{tenant_id}:")));
    }

    async fn invalidate_all(&self) {
        let mut guard = self.inner.write().await;
        guard.clear();
    }
}

// Test-only helpers — let unit tests in `enforce.rs` pre-seed the
// cache without touching the DB.
impl ArgumentPolicyCache {
    #[cfg(any(test, feature = "db-tests"))]
    pub(crate) async fn inner_write_for_test(
        &self,
    ) -> tokio::sync::RwLockWriteGuard<'_, HashMap<String, Entry>> {
        self.inner.write().await
    }
}

#[cfg(any(test, feature = "db-tests"))]
pub(crate) fn test_only_make_entry(policies: Vec<ArgumentPolicy>) -> Entry {
    Entry {
        policies,
        loaded_at: Instant::now(),
    }
}

fn surface_str(surface: Surface) -> &'static str {
    match surface {
        Surface::LlmOutputToolCalls => "llm_output_tool_calls",
        Surface::McpInput => "mcp_input",
        Surface::SkillInput => "skill_input",
    }
}

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

#[derive(sqlx::FromRow)]
struct PolicyRow {
    id: String,
    tenant_id: String,
    name: String,
    surface: String,
    target: String,
    argument_key: String,
    analyzer: String,
    analyzer_config: Option<serde_json::Value>,
    rules: serde_json::Value,
    enabled: bool,
    severity: String,
}

/// A stored policy this build cannot apply.
///
/// Names the policy and what is wrong with it, and nothing else: the rules
/// and the configuration are an operator's and do not belong in a log line
/// or an error chain.
#[derive(Debug)]
struct UnloadablePolicy {
    policy_id: String,
    why: String,
}

impl std::fmt::Display for UnloadablePolicy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "argument policy {} {}", self.policy_id, self.why)
    }
}

impl std::error::Error for UnloadablePolicy {}

impl PolicyRow {
    /// The row as a policy, or an error when its rules cannot be decoded.
    ///
    /// This used to be `unwrap_or_default()`, so one
    /// rule this build could not read (a per-rule severity from a newer
    /// control plane, a non-list `values`, a hand-edited row) turned the
    /// whole policy into ZERO rules — it matched nothing, with no log line.
    /// The error makes the load fail, and a failed load refuses the call
    /// (`enforce`, rule `policy_unavailable`).
    fn into_policy(self) -> Result<ArgumentPolicy, sqlx::Error> {
        let surface = match self.surface.as_str() {
            "mcp_input" => Surface::McpInput,
            "skill_input" => Surface::SkillInput,
            _ => Surface::LlmOutputToolCalls,
        };
        // The policy's own severity is what a rule that states none takes
        // (`evaluate`), so it is read as it is stored: `approval_required`
        // holds the call for a person. A value this build does not know is
        // the most restrictive one.
        let severity = match self.severity.as_str() {
            "warn" => Severity::Warn,
            "approval_required" => Severity::ApprovalRequired,
            "deny" => Severity::Deny,
            unknown => {
                tracing::error!(
                    policy_id = %self.id,
                    tenant_id = %self.tenant_id,
                    severity = %unknown,
                    "argument policy has a severity this gateway does not know; \
                     it is enforced as deny"
                );
                Severity::Deny
            }
        };
        let rules = match serde_json::from_value(self.rules) {
            Ok(rules) => rules,
            // A disabled policy applies to nothing (`evaluate` skips it), so
            // what its rules say cannot matter: it loads with none, and
            // disabling stays the operator's way out of a policy that
            // refuses everything.
            Err(e) if !self.enabled => {
                tracing::warn!(
                    policy_id = %self.id,
                    tenant_id = %self.tenant_id,
                    cause = ?e.classify(),
                    "a disabled argument policy has rules this gateway cannot decode; \
                     it will refuse the calls it governs if it is enabled"
                );
                Vec::new()
            }
            Err(e) => {
                // The serde message quotes the offending value, so only its
                // category is logged.
                tracing::error!(
                    policy_id = %self.id,
                    tenant_id = %self.tenant_id,
                    cause = ?e.classify(),
                    "argument policy rules cannot be decoded — the policy load fails \
                     and calls it governs are refused (fail closed)"
                );
                return Err(sqlx::Error::Decode(Box::new(UnloadablePolicy {
                    policy_id: self.id,
                    why: "has rules this gateway cannot decode".to_string(),
                })));
            }
        };
        // The analysis limits a JSON policy states (`max_leaves`,
        // `max_depth`, `max_array_elements`) are an integer from 1 to a hard
        // ceiling each. One that is not makes the policy unloadable: the
        // load fails and the calls it governs are refused. Clamping it
        // would analyse under a bound the operator did not write. A
        // disabled policy analyses nothing, so its limits cannot matter.
        if self.enabled && self.analyzer == "json" {
            if let Err(invalid) = super::AnalysisLimits::from_config(self.analyzer_config.as_ref())
            {
                tracing::error!(
                    policy_id = %self.id,
                    tenant_id = %self.tenant_id,
                    key = invalid.key,
                    ceiling = invalid.ceiling,
                    "argument policy states an analysis limit this gateway does not accept — \
                     the policy load fails and calls it governs are refused (fail closed)"
                );
                return Err(sqlx::Error::Decode(Box::new(UnloadablePolicy {
                    policy_id: self.id,
                    why: format!("is not loadable: {invalid}"),
                })));
            }
        }
        Ok(ArgumentPolicy {
            id: self.id,
            tenant_id: self.tenant_id,
            name: self.name,
            surface,
            target: self.target,
            argument_key: self.argument_key,
            analyzer: self.analyzer,
            analyzer_config: self.analyzer_config,
            rules,
            enabled: self.enabled,
            severity,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::argument_policy::policy::Rule;

    fn row(rules: serde_json::Value) -> PolicyRow {
        PolicyRow {
            id: "ap-1".into(),
            tenant_id: "t".into(),
            name: "payments-threshold".into(),
            surface: "mcp_input".into(),
            target: "*".into(),
            argument_key: "*".into(),
            analyzer: "json".into(),
            analyzer_config: None,
            rules,
            enabled: true,
            severity: "deny".into(),
        }
    }

    /// A row whose rules cannot be decoded is an
    /// error, never a policy with zero rules — and the error names the
    /// policy, not its content.
    #[test]
    fn a_row_whose_rules_cannot_be_decoded_is_an_error_not_zero_rules() {
        let undecodable = [
            // A per-rule severity this build does not know.
            serde_json::json!([{"deny_if": "field_gt", "field": "amount", "value": 1000,
                                "severity": "secret-severity"}]),
            // `values` that is not a list.
            serde_json::json!([{"deny_if": "host_in", "values": "secret-host.example"}]),
            // Not a list of rules at all.
            serde_json::json!({"deny_if": "writes"}),
        ];
        for rules in undecodable {
            let err = row(rules.clone())
                .into_policy()
                .expect_err("undecodable rules must fail the load");
            let text = format!("{err} / {err:?}");
            assert!(text.contains("ap-1"), "the error names the policy: {text}");
            assert!(
                !text.contains("secret"),
                "the error must not carry the policy's content: {text}"
            );
        }

        let ok = row(serde_json::json!([{"deny_if": "writes", "severity": "approval_required"}]))
            .into_policy()
            .expect("a well-formed row decodes");
        assert_eq!(ok.rules.len(), 1);
        // No rules is a legitimate (empty) policy, not an error.
        assert!(
            row(serde_json::json!([]))
                .into_policy()
                .unwrap()
                .rules
                .is_empty()
        );
    }

    /// The policy's own severity is what a rule without one inherits
    /// (`evaluate`: the rule's, else the policy's), so it is read as it is
    /// stored: `approval_required` holds the call for a person, it does not
    /// refuse it. A value this build does not know is the most restrictive.
    #[test]
    fn the_policy_severity_is_read_as_stored_and_an_unknown_one_denies() {
        let severity_of = |stored: &str| {
            let mut r = row(serde_json::json!([{"deny_if": "writes"}]));
            r.severity = stored.into();
            r.into_policy().expect("the row decodes").severity
        };
        assert_eq!(severity_of("deny"), Severity::Deny);
        assert_eq!(severity_of("approval_required"), Severity::ApprovalRequired);
        assert_eq!(severity_of("warn"), Severity::Warn);
        for unknown in [
            "",
            "block",
            "Warn",
            "WARN",
            "approval-required",
            " warn",
            "notify",
        ] {
            assert_eq!(severity_of(unknown), Severity::Deny, "{unknown:?}");
        }
    }

    /// An analysis limit that is not an integer from 1 to its ceiling makes
    /// an enabled JSON policy unloadable; the error names the policy and
    /// the key. The same setting on a disabled policy, or on a policy whose
    /// analyzer does not read it, fails nothing.
    #[test]
    fn a_json_policy_with_an_unacceptable_analysis_limit_is_not_loadable() {
        use crate::services::argument_policy::AnalysisLimits;
        let with = |config: serde_json::Value| {
            let mut r = row(serde_json::json!([]));
            r.analyzer_config = Some(config);
            r
        };
        for (key, ceiling) in [
            ("max_leaves", AnalysisLimits::CEILING.max_leaves),
            ("max_depth", AnalysisLimits::CEILING.max_depth),
            (
                "max_array_elements",
                AnalysisLimits::CEILING.max_array_elements,
            ),
        ] {
            for bad in [
                serde_json::json!(ceiling + 1),
                serde_json::json!(0),
                serde_json::json!(-4),
                serde_json::json!(2.5),
                serde_json::json!("64"),
            ] {
                let err = with(serde_json::json!({ key: bad.clone() }))
                    .into_policy()
                    .expect_err("an unacceptable limit must fail the load");
                let text = err.to_string();
                assert!(text.contains("ap-1") && text.contains(key), "{text}");

                let mut disabled = with(serde_json::json!({ key: bad.clone() }));
                disabled.enabled = false;
                assert!(disabled.into_policy().is_ok(), "{key}: disabled");

                let mut sql = with(serde_json::json!({ key: bad }));
                sql.analyzer = "sql".into();
                assert!(sql.into_policy().is_ok(), "{key}: not read by sql");
            }
            // At the ceiling and at one the policy loads.
            for good in [ceiling, 1] {
                assert!(
                    with(serde_json::json!({ key: good })).into_policy().is_ok(),
                    "{key}: {good}"
                );
            }
        }
    }

    /// A DISABLED policy applies to nothing, so its rules
    /// are not what stands between a call and its upstream: a disabled row
    /// that cannot be decoded must not fail the load (and with it every call
    /// of the group on that surface). Disabling is the operator's remedy for
    /// a policy that refuses everything; it has to work.
    #[test]
    fn a_disabled_row_that_cannot_be_decoded_does_not_fail_the_load() {
        let mut disabled = row(
            serde_json::json!([{"deny_if": "field_gt", "field": "amount",
            "value": 1000, "severity": "secret-severity"}]),
        );
        disabled.enabled = false;
        let policy = disabled
            .into_policy()
            .expect("a disabled policy cannot refuse a call");
        assert!(!policy.enabled);
        assert!(policy.rules.is_empty());
    }

    #[tokio::test]
    async fn invalidate_tenant_drops_only_matching_keys() {
        let cache = ArgumentPolicyCache::new(300);
        {
            let mut g = cache.inner.write().await;
            g.insert(
                "t1:g1:mcp_input".into(),
                Entry {
                    policies: vec![],
                    loaded_at: Instant::now(),
                },
            );
            g.insert(
                "t2:g1:mcp_input".into(),
                Entry {
                    policies: vec![],
                    loaded_at: Instant::now(),
                },
            );
        }
        cache.invalidate_tenant("t1").await;
        let g = cache.inner.read().await;
        assert!(!g.contains_key("t1:g1:mcp_input"));
        assert!(g.contains_key("t2:g1:mcp_input"));
    }

    #[tokio::test]
    async fn invalidate_all_drops_everything() {
        let cache = ArgumentPolicyCache::new(300);
        {
            let mut g = cache.inner.write().await;
            g.insert(
                "t1:g1:mcp_input".into(),
                Entry {
                    policies: vec![],
                    loaded_at: Instant::now(),
                },
            );
        }
        cache.invalidate_all().await;
        assert!(cache.inner.read().await.is_empty());
    }

    #[tokio::test]
    async fn cache_clone_shares_state_with_original() {
        // The pub/sub invalidator clones the cache; both handles must
        // see the same internal state or invalidations evict a phantom
        // and the request pipeline serves stale data.
        let a = ArgumentPolicyCache::new(300);
        let b = a.clone();
        {
            let mut g = a.inner.write().await;
            g.insert(
                "t:g:mcp_input".into(),
                Entry {
                    policies: vec![ArgumentPolicy {
                        id: "p".into(),
                        tenant_id: "t".into(),
                        name: "n".into(),
                        surface: Surface::McpInput,
                        target: "*".into(),
                        argument_key: "x".into(),
                        analyzer: "sql".into(),
                        analyzer_config: None,
                        rules: vec![Rule {
                            deny_if: Some("writes".into()),
                            allow_only_if: None,
                            values: None,
                            field: None,
                            value: None,
                            severity: None,
                            message: None,
                        }],
                        enabled: true,
                        severity: Severity::Deny,
                    }],
                    loaded_at: Instant::now(),
                },
            );
        }
        b.invalidate_tenant("t").await;
        assert!(
            a.inner.read().await.is_empty(),
            "drop via clone must affect original"
        );
    }
}
