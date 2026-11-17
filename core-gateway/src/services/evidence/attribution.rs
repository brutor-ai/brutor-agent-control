// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! (tenant, resource group) → AI System + active contract, cached.
//!
//! A row carries only `resource_group_id` (there is no
//! `ai_system_id` column). The record needs the system and the contract in
//! force so `system`, `epoch` and the contract reference are on the sealed
//! bytes. Resolved per distinct pair per flush through a process-wide cache
//! with the same shape and the same database-keyed cache key as
//! `compliance::load_for_audit` (`sqlx::test` databases share
//! the process).

use moka::future::Cache;
use serde_json::Value;
use sqlx::PgPool;
use std::collections::HashMap;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Attribution {
    pub ai_system_id: String,
    pub contract_id: Option<String>,
    pub contract_hash: Option<String>,
    pub contract_version: Option<i64>,
}

type Key = (String, String, Option<String>);

fn cache() -> &'static Cache<Key, Option<Arc<Attribution>>> {
    static C: OnceLock<Cache<Key, Option<Arc<Attribution>>>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(50_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

/// A database error is returned and not cached: a record sealed as
/// unattributed, or without its contract, is a false statement, so the
/// caller leaves the row unsealed (a counted capture gap) for backfill.
pub async fn resolve(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: Option<&str>,
) -> Result<Option<Arc<Attribution>>, sqlx::Error> {
    let Some(rg) = rg_id else {
        return Ok(None);
    };
    let db_key = pool
        .connect_options()
        .get_database()
        .unwrap_or("")
        .to_string();
    let key = (db_key, tenant_id.to_string(), Some(rg.to_string()));
    if let Some(hit) = cache().get(&key).await {
        return Ok(hit);
    }
    let loaded = load(pool, tenant_id, rg).await?.map(Arc::new);
    cache().insert(key, loaded.clone()).await;
    Ok(loaded)
}

async fn load(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: &str,
) -> Result<Option<Attribution>, sqlx::Error> {
    let Some((ai_system_id, _kind)) =
        crate::services::ai_system::nearest_ai_system(pool, tenant_id, rg_id).await?
    else {
        return Ok(None);
    };
    // `version` is INTEGER; cast so it decodes as i64. A decode error here
    // used to be swallowed, silently stripping `system.contract_*` and
    // `epoch` from every record. It is now returned: a record sealed
    // without the contract in force misstates what governed the action.
    let contract: Option<(String, Option<String>, Option<i64>)> = match sqlx::query_as(
        "SELECT id, contract_hash, version::bigint FROM ai_system_contracts \
         WHERE tenant_id = $1 AND ai_system_id = $2 AND status = 'active' \
         ORDER BY version DESC LIMIT 1",
    )
    .bind(tenant_id)
    .bind(&ai_system_id)
    .fetch_optional(pool)
    .await
    {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(
                target = "brutor::evidence",
                tenant_id,
                ai_system_id = %ai_system_id,
                error = %e,
                "active contract lookup failed; the rows stay unsealed for backfill",
            );
            return Err(e);
        }
    };
    let (contract_id, contract_hash, contract_version) = match contract {
        Some((id, h, v)) => (Some(id), h, v),
        None => (None, None, None),
    };
    Ok(Some(Attribution {
        ai_system_id,
        contract_id,
        contract_hash,
        contract_version,
    }))
}

// ---------------------------------------------------------------------------
// Profile-driven evidence options
// ---------------------------------------------------------------------------

/// What the evidence layer does for rows attributed to one (tenant, RG).
///
/// Resolved from the AI System's compliance profile
/// (`ai_system_compliance_profiles.core.{two_phase_actions,
/// inference_records, salt_digests}`) — a null/absent flag defaults to
/// "on iff the system carries a high-risk classification in any
/// framework" — plus the tenant's `evidence_settings.salt_digests_default`
/// and the RG's sensitivity tags. Ungoverned traffic gets the tenant salt
/// default and nothing else.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct EvidenceOptions {
    /// Seal a `planned` record at dispatch, confirmed by the outcome.
    pub two_phase_actions: bool,
    /// Seal LLM completions (`executed` / `errored`), not only refusals.
    pub inference_records: bool,
    /// Salt every content digest on the record (per-record salt).
    pub salt_digests: bool,
    /// The row's resource group (or its AI System) is tagged sensitive —
    /// `phi_resource` or `sensitive_data` — so read-only tools seal too.
    pub rg_sensitive: bool,
}

/// The raw profile inputs, pure so the defaulting rule is unit-testable.
#[derive(Debug, Clone, Default)]
pub struct ProfileInputs {
    pub core: Option<Value>,
    pub frameworks: Option<Value>,
    pub legacy_eu_risk_tier: Option<String>,
    pub tenant_salt_default: bool,
    pub rg_sensitive: bool,
    pub governed: bool,
}

/// High-risk in any framework: a `risk_tier: "high"` in any framework block
/// (EU AI Act first; Colorado / Korea carry the same key), or the legacy
/// `resource_groups.eu_ai_act_risk_tier` column the profile supersedes.
pub fn is_high_risk(frameworks: Option<&Value>, legacy_eu_risk_tier: Option<&str>) -> bool {
    if legacy_eu_risk_tier
        .map(|t| t.eq_ignore_ascii_case("high"))
        .unwrap_or(false)
    {
        return true;
    }
    frameworks
        .and_then(Value::as_object)
        .map(|fw| {
            fw.values().any(|block| {
                block
                    .get("risk_tier")
                    .and_then(Value::as_str)
                    .map(|t| t.eq_ignore_ascii_case("high"))
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
}

pub fn derive_options(i: &ProfileInputs) -> EvidenceOptions {
    if !i.governed {
        return EvidenceOptions {
            salt_digests: i.tenant_salt_default,
            rg_sensitive: i.rg_sensitive,
            ..Default::default()
        };
    }
    let high = is_high_risk(i.frameworks.as_ref(), i.legacy_eu_risk_tier.as_deref());
    let flag = |k: &str, default: bool| -> bool {
        i.core
            .as_ref()
            .and_then(|c| c.get(k))
            .and_then(Value::as_bool)
            .unwrap_or(default)
    };
    EvidenceOptions {
        two_phase_actions: flag("two_phase_actions", high),
        inference_records: flag("inference_records", high),
        salt_digests: flag("salt_digests", high || i.tenant_salt_default),
        rg_sensitive: i.rg_sensitive,
    }
}

fn options_cache() -> &'static Cache<Key, EvidenceOptions> {
    static C: OnceLock<Cache<Key, EvidenceOptions>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(50_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

fn db_key(pool: &PgPool) -> String {
    pool.connect_options()
        .get_database()
        .unwrap_or("")
        .to_string()
}

/// Options for rows of (tenant, RG). Cached like [`resolve`]. A missing
/// table (`evidence_settings`) reads as the default; a database
/// error is returned and not cached: options derived from it read as
/// "not governed", and the rows would go unsealed, unsalted or without their
/// planned record with nothing to show for it.
pub async fn options(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: Option<&str>,
) -> Result<EvidenceOptions, sqlx::Error> {
    let key = (
        db_key(pool),
        tenant_id.to_string(),
        rg_id.map(str::to_owned),
    );
    if let Some(hit) = options_cache().get(&key).await {
        return Ok(hit);
    }
    let attr = resolve(pool, tenant_id, rg_id).await?;
    let mut inputs = ProfileInputs {
        tenant_salt_default: tenant_salt_default(pool, tenant_id).await?,
        governed: attr.is_some(),
        ..Default::default()
    };
    if let Some(rg) = rg_id {
        inputs.rg_sensitive = rg_sensitive(pool, tenant_id, rg).await?;
    }
    if let Some(a) = &attr {
        if !inputs.rg_sensitive && Some(a.ai_system_id.as_str()) != rg_id {
            inputs.rg_sensitive = rg_sensitive(pool, tenant_id, &a.ai_system_id).await?;
        }
        let profile: Option<(Value, Value)> = sqlx::query_as(
            "SELECT core, frameworks FROM ai_system_compliance_profiles \
             WHERE tenant_id = $1 AND ai_system_id = $2",
        )
        .bind(tenant_id)
        .bind(&a.ai_system_id)
        .fetch_optional(pool)
        .await?;
        if let Some((core, frameworks)) = profile {
            inputs.core = Some(core);
            inputs.frameworks = Some(frameworks);
        }
        inputs.legacy_eu_risk_tier = sqlx::query_scalar::<_, Option<String>>(
            "SELECT eu_ai_act_risk_tier FROM resource_groups WHERE id = $1 AND tenant_id = $2",
        )
        .bind(&a.ai_system_id)
        .bind(tenant_id)
        .fetch_optional(pool)
        .await?
        .flatten();
    }
    let opts = derive_options(&inputs);
    options_cache().insert(key, opts).await;
    Ok(opts)
}

async fn tenant_salt_default(pool: &PgPool, tenant_id: &str) -> Result<bool, sqlx::Error> {
    // `to_regclass` keeps a pre-a178 schema quiet: no table ⇒ no row ⇒ the
    // default, without a failed statement in the server log every minute.
    let exists: Option<String> = sqlx::query_scalar::<_, Option<String>>(
        "SELECT to_regclass('public.evidence_settings')::text",
    )
    .fetch_one(pool)
    .await?;
    if exists.is_none() {
        return Ok(false);
    }
    Ok(sqlx::query_scalar::<_, Option<bool>>(
        "SELECT salt_digests_default FROM evidence_settings WHERE tenant_id = $1",
    )
    .bind(tenant_id)
    .fetch_optional(pool)
    .await?
    .flatten()
    .unwrap_or(false))
}

/// `phi_resource` or the `sensitive_data` flag. Both are
/// `NOT NULL DEFAULT false` columns. A failed lookup is returned: read
/// as "not sensitive" it let read-only tools on a sensitive system go
/// unsealed.
async fn rg_sensitive(pool: &PgPool, tenant_id: &str, rg_id: &str) -> Result<bool, sqlx::Error> {
    Ok(sqlx::query_scalar::<_, bool>(
        "SELECT (phi_resource OR sensitive_data) \
         FROM resource_groups WHERE id = $1 AND tenant_id = $2",
    )
    .bind(rg_id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await?
    .unwrap_or(false))
}

// ---------------------------------------------------------------------------
// Tool annotations (the consequential rule)
// ---------------------------------------------------------------------------

type ToolKey = (String, String, String);

fn tool_cache() -> &'static Cache<ToolKey, Arc<HashMap<String, bool>>> {
    static C: OnceLock<Cache<ToolKey, Arc<HashMap<String, bool>>>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(20_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

/// `readOnlyHint` of `tool` on MCP server `server_id`, from the server's
/// discovered capabilities. `None` when the server or the tool is unknown —
/// which the consequential rule treats as a command (sealed). A database
/// error is returned and not cached.
pub async fn tool_read_only(
    pool: &PgPool,
    tenant_id: &str,
    server_id: &str,
    tool: &str,
) -> Result<Option<bool>, sqlx::Error> {
    let key = (db_key(pool), tenant_id.to_string(), server_id.to_string());
    let map = match tool_cache().get(&key).await {
        Some(m) => m,
        None => {
            let caps: Option<Option<Value>> = sqlx::query_scalar(
                "SELECT capabilities::jsonb FROM mcp_servers WHERE id = $1 AND tenant_id = $2",
            )
            .bind(server_id)
            .bind(tenant_id)
            .fetch_optional(pool)
            .await?;
            let m = Arc::new(read_only_map(caps.flatten().as_ref()));
            tool_cache().insert(key, m.clone()).await;
            m
        }
    };
    Ok(map.get(tool).copied())
}

/// tool name → `readOnlyHint` (spec default `false` when annotations exist
/// without the hint). Accepts both capability shapes the proxy reads
/// (`{"tools": [...]}` and a bare array).
pub fn read_only_map(caps: Option<&Value>) -> HashMap<String, bool> {
    let tools = caps.and_then(|c| {
        c.get("tools")
            .and_then(Value::as_array)
            .or_else(|| c.as_array())
    });
    let mut out = HashMap::new();
    for t in tools.into_iter().flatten() {
        if let Some(name) = t.get("name").and_then(Value::as_str) {
            let ro = t
                .get("annotations")
                .and_then(|a| a.get("readOnlyHint"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            out.insert(name.to_owned(), ro);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Notice records by conversation
// ---------------------------------------------------------------------------

type NoticeKey = (String, String, String);

fn notice_cache() -> &'static Cache<NoticeKey, String> {
    static C: OnceLock<Cache<NoticeKey, String>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(100_000)
            .time_to_live(Duration::from_secs(6 * 3600))
            .build()
    })
}

/// Remember the notice record sealed for a conversation (called by the
/// portal notice endpoint, so the same replica never has to look it up).
pub async fn remember_notice(pool: &PgPool, tenant_id: &str, handle: &str, record_id: &str) {
    notice_cache()
        .insert(
            (db_key(pool), tenant_id.to_string(), handle.to_string()),
            record_id.to_string(),
        )
        .await;
}

/// The earliest notice record sealed for `handle`, if any. Positive answers
/// are cached; a miss is not, because the notice may land a moment later
/// and the conversation's first turn is the one that must cite it.
///
/// A database error is returned: the record must not be sealed as
/// citing no notice when one may exist.
pub async fn notice_for(
    pool: &PgPool,
    tenant_id: &str,
    handle: &str,
) -> Result<Option<String>, sqlx::Error> {
    let key = (db_key(pool), tenant_id.to_string(), handle.to_string());
    if let Some(hit) = notice_cache().get(&key).await {
        return Ok(Some(hit));
    }
    let found: Option<String> = sqlx::query_scalar(
        "SELECT record_id FROM action_records \
         WHERE tenant_id = $1 AND verdict = 'recorded' \
           AND record->'context'->>'record_type' = 'ai.brutor.notice' \
           AND record->'context'->>'conversation_handle' = $2 \
         ORDER BY seq ASC LIMIT 1",
    )
    .bind(tenant_id)
    .bind(handle)
    .fetch_optional(pool)
    .await?;
    if let Some(id) = &found {
        notice_cache().insert(key, id.clone()).await;
    }
    Ok(found)
}

#[cfg(test)]
pub fn invalidate_all() {
    cache().invalidate_all();
    options_cache().invalidate_all();
    tool_cache().invalidate_all();
    notice_cache().invalidate_all();
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn governed(core: Value, frameworks: Value) -> ProfileInputs {
        ProfileInputs {
            core: Some(core),
            frameworks: Some(frameworks),
            governed: true,
            ..Default::default()
        }
    }

    #[test]
    fn null_flags_default_to_the_high_risk_classification() {
        let low = derive_options(&governed(json!({}), json!({})));
        assert!(!low.two_phase_actions && !low.inference_records && !low.salt_digests);

        let high = derive_options(&governed(
            json!({"two_phase_actions": null}),
            json!({"eu-ai-act": {"risk_tier": "high"}}),
        ));
        assert!(high.two_phase_actions && high.inference_records && high.salt_digests);

        // any framework's high classification counts
        let co = derive_options(&governed(
            json!({}),
            json!({"colorado-sb-24-205": {"risk_tier": "high"}}),
        ));
        assert!(co.two_phase_actions);

        // the legacy RG column still counts
        let mut legacy = governed(json!({}), json!({}));
        legacy.legacy_eu_risk_tier = Some("HIGH".into());
        assert!(derive_options(&legacy).inference_records);
    }

    #[test]
    fn explicit_flags_win_over_the_default() {
        let o = derive_options(&governed(
            json!({"two_phase_actions": false, "inference_records": true, "salt_digests": false}),
            json!({"eu-ai-act": {"risk_tier": "high"}}),
        ));
        assert!(!o.two_phase_actions);
        assert!(o.inference_records);
        assert!(!o.salt_digests);
    }

    #[test]
    fn tenant_salt_default_applies_when_the_profile_is_silent() {
        let mut i = governed(json!({}), json!({}));
        i.tenant_salt_default = true;
        assert!(derive_options(&i).salt_digests);
        i.core = Some(json!({"salt_digests": false}));
        assert!(!derive_options(&i).salt_digests, "explicit false wins");
        let ungoverned = ProfileInputs {
            tenant_salt_default: true,
            rg_sensitive: true,
            ..Default::default()
        };
        let o = derive_options(&ungoverned);
        assert!(o.salt_digests && o.rg_sensitive);
        assert!(!o.two_phase_actions && !o.inference_records);
    }

    #[test]
    fn read_only_map_reads_both_capability_shapes() {
        let wrapped = json!({"tools": [
            {"name": "list_orders", "annotations": {"readOnlyHint": true}},
            {"name": "write_order", "annotations": {"destructiveHint": true}},
            {"name": "bare"}
        ]});
        let m = read_only_map(Some(&wrapped));
        assert_eq!(m.get("list_orders"), Some(&true));
        assert_eq!(m.get("write_order"), Some(&false));
        assert_eq!(m.get("bare"), Some(&false));
        assert_eq!(m.get("unknown"), None);
        let bare = json!([{"name": "q", "annotations": {"readOnlyHint": true}}]);
        assert_eq!(read_only_map(Some(&bare)).get("q"), Some(&true));
        assert!(read_only_map(None).is_empty());
    }
}
