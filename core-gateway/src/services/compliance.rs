// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Compliance framework tagging.
//!
//! Mirror of the Python tagging engine at
//! `brutor-gateway-control/app/services/compliance/tagging.py`.
//! The two MUST stay in lockstep — any change to one needs the
//! drift test in `test_a070_compliance_mapping_migration.py` and
//! the per-framework unit tests below to be updated together.
//!
//! Why two implementations rather than IPC?
//!
//! - The audit-row write path runs ~10k+ times/sec under load; a
//!   Python callback per row would dominate runtime cost. The
//!   Python copy stays around for backfill jobs and for unit
//!   tests where the runtime isn't running.
//! - Compliance taxonomies churn rarely (SOC 2 CC numbering, EU
//!   AI Act tiers — quarterly at most). Coordinated changes
//!   across two languages is acceptable cost for the speed gain.
//!
//! Public surface:
//!
//!   * [`ComplianceProfile`] — parses tenant.compliance_profile
//!     JSONB. Empty `frameworks` short-circuits derive_tags.
//!   * [`TaggingContext`] — input record. Audit writer
//!     populates it from the in-flight ProxyLog + cached tenant
//!     and RG rows.
//!   * [`derive_tags`] — pure function returning the tag list
//!     for one call. `None` = no profile (writes NULL to the
//!     audit column). `Some([])` = profile present but quiet
//!     call. `Some(vec)` = tagged.

use std::sync::OnceLock;
use std::time::Duration;

use moka::future::Cache;
use serde::Deserialize;
use sqlx::PgPool;

// Tag string constants — mirror of `app/services/compliance/
// tagging.py`. The drift suite asserts these stay in sync via
// regex match against the Python source.

pub const TAG_SOC2_AUTH_EVENT: &str = "soc2.cc6.1";
pub const TAG_SOC2_GUARDRAIL_BLOCK: &str = "soc2.cc7.2";
pub const TAG_SOC2_POLICY_DENY: &str = "soc2.cc6.6";
pub const TAG_SOC2_RESIDENCY_BLOCK: &str = "soc2.cc6.7";
pub const TAG_SOC2_RATE_LIMIT_HIT: &str = "soc2.cc7.1";
pub const TAG_GDPR_ART_30: &str = "gdpr_art_30";
pub const TAG_HIPAA_PHI: &str = "hipaa.phi";
pub const TAG_HIPAA_RG_MARKED: &str = "hipaa.rg_marked";
pub const TAG_ISO_42001: &str = "iso_42001";

// Framework keys — the pinned set Python exposes via
// `FRAMEWORK_KEYS` in `app/api/v1/models/admin/compliance.py`.
pub const FRAMEWORK_GDPR_ART_30: &str = "gdpr_art_30";
pub const FRAMEWORK_SOC2: &str = "soc2";
pub const FRAMEWORK_HIPAA: &str = "hipaa";
pub const FRAMEWORK_EU_AI_ACT: &str = "eu_ai_act";
pub const FRAMEWORK_ISO_42001: &str = "iso_42001";

/// Parsed per-tenant compliance profile.
///
/// Pulled out of the `tenants.compliance_profile` JSONB column.
/// An empty `frameworks` list — the default for tenants who
/// haven't opted into compliance tagging — makes
/// [`derive_tags`] return `None`, and the audit writer skips the
/// `compliance_tags` column entirely.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ComplianceProfile {
    #[serde(default)]
    pub frameworks: Vec<String>,
}

impl ComplianceProfile {
    /// True iff the tenant has declared the given framework. Kept
    /// public so downstream callers (e.g. a future cache layer)
    /// can ask the question without re-implementing the linear
    /// scan.
    #[allow(dead_code)]
    pub fn has(&self, framework: &str) -> bool {
        self.frameworks.iter().any(|f| f == framework)
    }
}

/// Per-call input to the tagging engine.
///
/// Mirrors `TaggingContext` in the Python implementation. The
/// audit writer populates this from the in-flight ProxyLog,
/// plus the cached tenant + RG rows.
#[derive(Debug, Clone, Default)]
pub struct TaggingContext<'a> {
    pub frameworks: &'a [String],

    pub eu_ai_act_risk_tier: Option<&'a str>,
    pub rg_phi_resource: bool,

    pub has_pii: bool,
    pub has_phi: bool,
    pub gateway_user_id: Option<&'a str>,
    pub idp_user_email: Option<&'a str>,

    pub is_auth_event: bool,
    pub guardrail_blocked: bool,
    pub argument_policy_denied: bool,
    pub residency_blocked: bool,
    pub rate_limit_hit: bool,
}

impl<'a> TaggingContext<'a> {
    fn lists(&self, framework: &str) -> bool {
        self.frameworks.iter().any(|f| f == framework)
    }

    fn data_subject_identifiable(&self) -> bool {
        self.gateway_user_id.is_some() || self.idp_user_email.is_some()
    }
}

// ---------------------------------------------------------------------------
// Per-framework derivation
// ---------------------------------------------------------------------------

fn tag_gdpr(ctx: &TaggingContext, out: &mut Vec<String>) {
    if !ctx.lists(FRAMEWORK_GDPR_ART_30) {
        return;
    }
    if !ctx.has_pii {
        return;
    }
    if !ctx.data_subject_identifiable() {
        // Same carve-out as the Python: anonymous PII calls don't
        // enter the RoPA register.
        return;
    }
    out.push(TAG_GDPR_ART_30.to_string());
}

fn tag_eu_ai_act(ctx: &TaggingContext, out: &mut Vec<String>) {
    if !ctx.lists(FRAMEWORK_EU_AI_ACT) {
        return;
    }
    if let Some(tier) = ctx.eu_ai_act_risk_tier {
        out.push(format!("eu_ai_act.{tier}"));
    }
}

fn tag_soc2(ctx: &TaggingContext, out: &mut Vec<String>) {
    if !ctx.lists(FRAMEWORK_SOC2) {
        return;
    }
    if ctx.is_auth_event {
        out.push(TAG_SOC2_AUTH_EVENT.to_string());
    }
    if ctx.guardrail_blocked {
        out.push(TAG_SOC2_GUARDRAIL_BLOCK.to_string());
    }
    if ctx.argument_policy_denied {
        out.push(TAG_SOC2_POLICY_DENY.to_string());
    }
    if ctx.residency_blocked {
        out.push(TAG_SOC2_RESIDENCY_BLOCK.to_string());
    }
    if ctx.rate_limit_hit {
        out.push(TAG_SOC2_RATE_LIMIT_HIT.to_string());
    }
}

fn tag_hipaa(ctx: &TaggingContext, out: &mut Vec<String>) {
    if !ctx.lists(FRAMEWORK_HIPAA) {
        return;
    }
    if ctx.has_phi {
        out.push(TAG_HIPAA_PHI.to_string());
    }
    if ctx.rg_phi_resource {
        out.push(TAG_HIPAA_RG_MARKED.to_string());
    }
}

fn tag_iso_42001(ctx: &TaggingContext, out: &mut Vec<String>) {
    if !ctx.lists(FRAMEWORK_ISO_42001) {
        return;
    }
    out.push(TAG_ISO_42001.to_string());
}

// ---------------------------------------------------------------------------
// Engine entrypoint
// ---------------------------------------------------------------------------

/// Compute the compliance_tags array for one call.
///
/// Returns `None` when the tenant has no compliance profile (no
/// frameworks declared). The audit writer maps `None` to NULL in
/// `proxy_logs.compliance_tags`, which is the "engine never ran"
/// sentinel — distinct from `Some(vec![])` ("engine ran, quiet
/// call"), which the dashboard uses to surface compliant-but-
/// uneventful tenants.
pub fn derive_tags(ctx: &TaggingContext) -> Option<Vec<String>> {
    if ctx.frameworks.is_empty() {
        return None;
    }
    let mut tags = Vec::new();
    tag_gdpr(ctx, &mut tags);
    tag_eu_ai_act(ctx, &mut tags);
    tag_soc2(ctx, &mut tags);
    tag_hipaa(ctx, &mut tags);
    tag_iso_42001(ctx, &mut tags);
    Some(tags)
}

/// Load the tenant's compliance profile from Postgres. Returns
/// `None` if the row is missing (defensive — should not happen
/// for live traffic) or the profile is empty (frameworks=[],
/// which the engine short-circuits anyway).
///
/// This is the raw DB lookup. `load_for_audit` is the cached
/// entry point the audit writer uses — call that, not this, from
/// anything on a per-row path. Mirrors `services::residency::
/// load_tenant_profile`.
///
/// A database error is returned, never read as "no profile".
pub async fn load_tenant_profile(
    pool: &PgPool,
    tenant_id: &str,
) -> Result<Option<ComplianceProfile>, sqlx::Error> {
    let row: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT compliance_profile FROM tenants WHERE id = $1 LIMIT 1")
            .bind(tenant_id)
            .fetch_optional(pool)
            .await?;
    Ok(row
        .and_then(|(v,)| v)
        .and_then(|raw| parse_profile(Some(&raw))))
}

/// Parse a JSONB value into a ComplianceProfile. Returns `None`
/// on malformed JSON or an empty frameworks list — both treated
/// as "no enforcement" so a misconfigured tenant doesn't get
/// every call tagged with garbage.
pub fn parse_profile(raw: Option<&serde_json::Value>) -> Option<ComplianceProfile> {
    let v = raw?;
    if v.is_null() {
        return None;
    }
    match serde_json::from_value::<ComplianceProfile>(v.clone()) {
        Ok(p) if !p.frameworks.is_empty() => Some(p),
        Ok(_) => None,
        Err(e) => {
            tracing::warn!(
                target = "brutor::compliance",
                error = %e,
                "compliance_profile JSONB malformed; treating as no-profile"
            );
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Background-writer integration
// ---------------------------------------------------------------------------

/// Resource-group compliance fields read alongside the tenant
/// profile. Optional because not every audit row carries a
/// resource_group_id (system-server / boot-time rows).
#[derive(Debug, Clone, Default)]
pub struct RgComplianceFields {
    pub eu_ai_act_risk_tier: Option<String>,
    pub phi_resource: bool,
}

/// Load the tenant compliance_profile + RG fields needed by the
/// engine. Single query that joins both because the engine
/// always needs both when the profile lists `eu_ai_act` or
/// `hipaa`.
///
/// Returns `None` when the tenant has no profile (no frameworks
/// declared). The caller (background_writer) treats this as the
/// "engine never ran" sentinel and skips the column entirely.
///
/// Cached per `(tenant_id, resource_group_id)` for 60 s.
///
/// This is called from `flush_logs`, which is the
/// single-threaded log consumer the whole batching design exists
/// to keep fast — one flush of 500 rows spanning 40 distinct
/// (tenant, RG) pairs used to pay 40-80 sequential round trips,
/// because the writer's memo was rebuilt for every batch. The
/// sibling lookup one block below it in the same function
/// (`data_retention::load_store_bodies`) has had a process-wide
/// moka cache with exactly this TTL; this brings the
/// two into line rather than leaving adjacent code with two
/// different caching stories.
///
/// The trade: a compliance-profile or risk-tier edit takes up to
/// 60 s to show up in newly-tagged rows, the same window
/// retention already accepts. Tags are descriptive metadata on an
/// audit row, not an enforcement gate, so a minute of staleness
/// costs an operator nothing and mis-tags nothing that was
/// already written. `invalidate` shortens it to zero when a
/// caller knows the profile changed.
pub async fn load_for_audit(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: Option<&str>,
) -> Result<Option<(ComplianceProfile, RgComplianceFields)>, sqlx::Error> {
    // The key includes the DATABASE this pool points at.
    //
    // The cache is process-global while the thing it caches is per-database
    // state. In production that distinction is invisible: one process, one
    // database, so the extra key component is a constant. Under `sqlx::test`
    // it is the whole ballgame — every test gets its own throwaway database
    // but they share a process and reuse tenant ids, so a tenant-only key let
    // one test's "no compliance profile" answer a different test running
    // against a different database. That surfaced as compliance tags coming
    // back empty in a test whose own database plainly had the profile, which
    // is a confusing way to learn that a cache key was under-specified.
    let db_key = pool
        .connect_options()
        .get_database()
        .unwrap_or("")
        .to_string();
    let key = (db_key, tenant_id.to_string(), rg_id.map(str::to_string));
    if let Some(hit) = audit_cache().get(&key).await {
        return Ok(hit);
    }
    // An error is returned and NOT cached: a cached failure left every
    // row of the tenant untagged for the TTL with nothing logged after the
    // first.
    let loaded = load_for_audit_uncached(pool, tenant_id, rg_id).await?;
    audit_cache().insert(key, loaded.clone()).await;
    Ok(loaded)
}

/// The `(tenant, resource_group) -> profile` cache behind
/// [`load_for_audit`].
///
/// A miss is cached too (`None` — "this tenant declares no
/// frameworks"), which is the common case for most deployments
/// and the one that would otherwise re-query on every single row.
/// Keyed `(database, tenant, resource_group)` — see `load_for_audit` for why
/// the database is part of the key.
type AuditCacheKey = (String, String, Option<String>);

fn audit_cache() -> &'static Cache<AuditCacheKey, Option<(ComplianceProfile, RgComplianceFields)>> {
    static CACHE: OnceLock<Cache<AuditCacheKey, Option<(ComplianceProfile, RgComplianceFields)>>> =
        OnceLock::new();
    CACHE.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

/// Drop cached profiles after a compliance-profile write.
///
/// Mirrors `data_retention::invalidate` in intent. Wider in effect: the key
/// is `(tenant, resource_group)` and there is no cheap way to enumerate one
/// tenant's resource groups from here, so this clears the whole map. That is
/// the right trade — the cost is a handful of re-reads on the next flush,
/// against the alternative of carrying a second index purely to make a
/// rare operator action marginally cheaper.
pub fn invalidate(_tenant_id: &str) {
    audit_cache().invalidate_all();
}

async fn load_for_audit_uncached(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: Option<&str>,
) -> Result<Option<(ComplianceProfile, RgComplianceFields)>, sqlx::Error> {
    let Some(profile) = load_tenant_profile(pool, tenant_id).await? else {
        return Ok(None);
    };

    // Cheap RG lookup only when the loaded profile lists a regime
    // that uses RG fields. ISO 42001 / SOC 2 / GDPR don't need
    // the RG row.
    let needs_rg = profile
        .frameworks
        .iter()
        .any(|f| f == FRAMEWORK_EU_AI_ACT || f == FRAMEWORK_HIPAA);
    if !needs_rg {
        return Ok(Some((profile, RgComplianceFields::default())));
    }

    let rg_fields = if let Some(rg) = rg_id {
        let row: Option<(Option<String>, bool)> = sqlx::query_as(
            "SELECT eu_ai_act_risk_tier, phi_resource \
             FROM resource_groups WHERE id = $1 AND tenant_id = $2 LIMIT 1",
        )
        .bind(rg)
        .bind(tenant_id)
        .fetch_optional(pool)
        .await?;
        match row {
            Some((tier, phi)) => RgComplianceFields {
                eu_ai_act_risk_tier: tier,
                phi_resource: phi,
            },
            None => RgComplianceFields::default(),
        }
    } else {
        RgComplianceFields::default()
    };

    Ok(Some((profile, rg_fields)))
}

/// Build a TaggingContext from log state + loaded profile and
/// derive the tag list. Audit-row state used:
///
///   * `log.residency_class == "blocked"` → residency_blocked
///   * `log.http_status_code == 429`      → rate_limit_hit
///   * `log.guardrail_blocked`            → guardrail_blocked
///   * `log.argument_policy_denied`       → argument_policy_denied
///   * `log.pii_detected`                 → has_pii (drives GDPR
///     + acts as a proxy for
///       has_phi when the
///       tenant has HIPAA on)
///   * `log.is_auth_event`                → is_auth_event
///   * `log.gateway_user_id` /
///     `log.idp_user_email`               → identifiable subject
///   * `iso_42001` always fires when declared
///
/// **HIPAA PHI semantics**: the schema doesn't separate
/// PHI from generic PII (would require duplicating the
/// detector's category enumeration in the audit row). Instead
/// the engine treats `pii_detected == true` as proxy for
/// `has_phi` when the tenant declares HIPAA — the conservative
/// reading. False positives go to the auditor's review queue;
/// false negatives are the unacceptable failure mode.
pub fn derive_tags_from_log_state(
    profile: &ComplianceProfile,
    rg_fields: &RgComplianceFields,
    log: &crate::models::db::ProxyLogInsert,
) -> Option<Vec<String>> {
    let residency_blocked = log
        .residency_class
        .as_deref()
        .map(|s| s == residency::CLASS_BLOCKED)
        .unwrap_or(false);
    let rate_limit_hit = log.http_status_code == Some(429);

    let ctx = TaggingContext {
        frameworks: &profile.frameworks,
        eu_ai_act_risk_tier: rg_fields.eu_ai_act_risk_tier.as_deref(),
        rg_phi_resource: rg_fields.phi_resource,
        has_pii: log.pii_detected,
        has_phi: log.pii_detected,
        gateway_user_id: log.gateway_user_id.as_deref(),
        idp_user_email: log.idp_user_email.as_deref(),
        is_auth_event: log.is_auth_event,
        guardrail_blocked: log.guardrail_blocked,
        argument_policy_denied: log.argument_policy_denied,
        residency_blocked,
        rate_limit_hit,
    };
    derive_tags(&ctx)
}

use crate::services::residency;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn frameworks(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn no_frameworks_returns_none() {
        let ctx = TaggingContext::default();
        assert!(derive_tags(&ctx).is_none());
    }

    #[test]
    fn declared_profile_but_quiet_call_returns_empty_vec() {
        let fws = frameworks(&["soc2"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            ..Default::default()
        };
        let tags = derive_tags(&ctx).expect("Some([])");
        assert!(tags.is_empty());
    }

    #[test]
    fn gdpr_fires_with_pii_and_user_id() {
        let fws = frameworks(&["gdpr_art_30"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            has_pii: true,
            gateway_user_id: Some("u-42"),
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), vec![TAG_GDPR_ART_30]);
    }

    #[test]
    fn gdpr_silent_without_pii() {
        let fws = frameworks(&["gdpr_art_30"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            has_pii: false,
            gateway_user_id: Some("u-42"),
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn gdpr_silent_without_identifiable_subject() {
        let fws = frameworks(&["gdpr_art_30"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            has_pii: true,
            gateway_user_id: None,
            idp_user_email: None,
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn gdpr_idp_email_satisfies_identifiability() {
        let fws = frameworks(&["gdpr_art_30"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            has_pii: true,
            idp_user_email: Some("user@example"),
            ..Default::default()
        };
        assert!(
            derive_tags(&ctx)
                .unwrap()
                .contains(&TAG_GDPR_ART_30.to_string())
        );
    }

    #[test]
    fn eu_ai_act_emits_tier_suffix() {
        let fws = frameworks(&["eu_ai_act"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            eu_ai_act_risk_tier: Some("high"),
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), vec!["eu_ai_act.high"]);
    }

    #[test]
    fn eu_ai_act_silent_when_rg_unclassified() {
        let fws = frameworks(&["eu_ai_act"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            eu_ai_act_risk_tier: None,
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn soc2_maps_each_enforcement_outcome_to_its_control() {
        let fws = frameworks(&["soc2"]);
        for (field, expected) in [
            ("auth", TAG_SOC2_AUTH_EVENT),
            ("guardrail", TAG_SOC2_GUARDRAIL_BLOCK),
            ("policy", TAG_SOC2_POLICY_DENY),
            ("residency", TAG_SOC2_RESIDENCY_BLOCK),
            ("rate_limit", TAG_SOC2_RATE_LIMIT_HIT),
        ] {
            let ctx = TaggingContext {
                frameworks: &fws,
                is_auth_event: field == "auth",
                guardrail_blocked: field == "guardrail",
                argument_policy_denied: field == "policy",
                residency_blocked: field == "residency",
                rate_limit_hit: field == "rate_limit",
                ..Default::default()
            };
            let tags = derive_tags(&ctx).unwrap();
            assert_eq!(
                tags,
                vec![expected.to_string()],
                "field {field} should map to {expected}"
            );
        }
    }

    #[test]
    fn hipaa_phi_and_rg_marked_emit_both_tags() {
        let fws = frameworks(&["hipaa"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            has_phi: true,
            rg_phi_resource: true,
            ..Default::default()
        };
        let tags = derive_tags(&ctx).unwrap();
        assert!(tags.contains(&TAG_HIPAA_PHI.to_string()));
        assert!(tags.contains(&TAG_HIPAA_RG_MARKED.to_string()));
    }

    #[test]
    fn iso_42001_always_fires_when_declared() {
        let fws = frameworks(&["iso_42001"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            ..Default::default()
        };
        assert_eq!(derive_tags(&ctx).unwrap(), vec![TAG_ISO_42001]);
    }

    #[test]
    fn kitchen_sink_cross_regime_call() {
        // Mirror of Python's TestMultiRegimeCall — single call
        // hitting every regime that should emit a tag from each.
        let fws = frameworks(&["gdpr_art_30", "soc2", "hipaa", "eu_ai_act", "iso_42001"]);
        let ctx = TaggingContext {
            frameworks: &fws,
            eu_ai_act_risk_tier: Some("high"),
            rg_phi_resource: true,
            has_pii: true,
            has_phi: true,
            gateway_user_id: Some("u-42"),
            is_auth_event: true,
            guardrail_blocked: true,
            ..Default::default()
        };
        let tags = derive_tags(&ctx).unwrap();
        assert!(tags.contains(&TAG_GDPR_ART_30.to_string()));
        assert!(tags.contains(&"eu_ai_act.high".to_string()));
        assert!(tags.contains(&TAG_SOC2_AUTH_EVENT.to_string()));
        assert!(tags.contains(&TAG_SOC2_GUARDRAIL_BLOCK.to_string()));
        assert!(tags.contains(&TAG_HIPAA_PHI.to_string()));
        assert!(tags.contains(&TAG_HIPAA_RG_MARKED.to_string()));
        assert!(tags.contains(&TAG_ISO_42001.to_string()));
        let mut sorted = tags.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), tags.len(), "engine emitted duplicate tags");
    }

    // -----------------------------------------------------------------
    // parse_profile
    // -----------------------------------------------------------------

    #[test]
    fn parse_profile_none_on_null_or_missing() {
        assert!(parse_profile(None).is_none());
        assert!(parse_profile(Some(&serde_json::Value::Null)).is_none());
    }

    #[test]
    fn parse_profile_none_when_frameworks_empty() {
        let raw = serde_json::json!({"frameworks": []});
        assert!(parse_profile(Some(&raw)).is_none());
    }

    #[test]
    fn parse_profile_extracts_frameworks() {
        let raw = serde_json::json!({"frameworks": ["soc2", "hipaa"]});
        let p = parse_profile(Some(&raw)).expect("should parse");
        assert_eq!(p.frameworks, vec!["soc2", "hipaa"]);
    }

    #[test]
    fn parse_profile_handles_malformed_jsonb() {
        // Wrong shape — fails Deserialize. parse_profile logs at
        // warn and returns None so a tenant misconfiguration is
        // fail-open, not fail-closed.
        let raw = serde_json::json!({"frameworks": "soc2"});
        assert!(parse_profile(Some(&raw)).is_none());
    }

    // -----------------------------------------------------------------
    // derive_tags_from_log_state — background-writer integration
    // -----------------------------------------------------------------

    fn make_log_with(
        residency_class: Option<&str>,
        status: Option<i32>,
        user_id: Option<&str>,
    ) -> crate::models::db::ProxyLogInsert {
        // Build a minimal ProxyLogInsert directly. We only need
        // the fields the tagging engine reads. Using
        // `..Default::default()` would require Default on
        // ProxyLogInsert which it doesn't have; instead spell
        // out the fields the engine touches and leave the rest
        // at sensible blanks.
        let mut log = crate::models::db::ProxyLogInsert {
            step_id: None,
            step_name: None,
            id: "test-log".into(),
            tenant_id: "test-tenant".into(),
            timestamp: chrono::Utc::now(),
            proxy_type: "llm".into(),
            proxy_subtype: None,
            skill_id: None,
            skill_execution_id: None,
            correlation_id: None,
            parent_request_id: None,
            request_id: None,
            end_user_session_id: "test-session".into(),
            gateway_user_id: None,
            gateway_user_username: None,
            idp_user_id: None,
            idp_user_username: None,
            idp_user_email: None,
            client_ip: None,
            user_agent: None,
            server_id: None,
            idp_server_id: None,
            resource_group_id: None,
            api_key_id: None,
            api_key_name: None,
            http_method: String::new(),
            endpoint: String::new(),
            target_url: None,
            request_size_bytes: None,
            request_headers: None,
            request_body_preview: None,
            http_status_code: None,
            response_size_bytes: None,
            response_headers: None,
            response_body_preview: None,
            success: true,
            error_message: None,
            error_type: None,
            duration_ms: None,
            mcp_method: None,
            mcp_request_id: None,
            mcp_capability_name: None,
            llm_model_requested: None,
            llm_model_used: None,
            llm_provider: None,
            llm_prompt_tokens: None,
            llm_completion_tokens: None,
            llm_total_tokens: None,
            llm_streaming: None,
            llm_temperature: None,
            llm_max_tokens: None,
            llm_function_calling: None,
            llm_used_fallback: None,
            llm_fallback_reason: None,
            llm_estimated_cost_usd: None,
            media_images_generated: None,
            media_audio_chars: None,
            media_audio_seconds_input: None,
            media_video_seconds_output: None,
            llm_cache_hit: None,
            llm_cache_layer: None,
            llm_tokens_saved: None,
            llm_cache_similarity_score: None,
            prompt_cache_write_tokens: None,
            prompt_cache_read_tokens: None,
            prompt_cache_savings_usd: None,
            rag_enabled: None,
            rag_chunks_retrieved: None,
            rag_context_tokens: None,
            rag_collection_ids: None,
            prompt_fingerprint: None,
            outcome_id: None,
            source: crate::services::otel_ingest::SOURCE_GATEWAY.to_string(),
            rag_top_similarity_score: None,
            rag_retrieval_duration_ms: None,
            oauth_flow_type: None,
            oauth_scope: None,
            root_task_id: None,
            delegation_depth: None,
            trace_root_kind: None,
            trace_continuity: None,
            run_end_state: None,
            region_routed_to: None,
            residency_class: None,
            compliance_tags: None,
            guardrail_blocked: false,
            argument_policy_denied: false,
            pii_detected: false,
            is_auth_event: false,
            current_task_id: None,
            parent_task_id: None,
            actor_agent_id: None,
            subject_id: None,
            subject_kind: None,
            agent_authz_decision: None,
            agent_grant_id: None,
            semantic_policy_id: None,
            // Stamped by audit_chain::stamp_batch at flush time, not
            // here: a row shed under backpressure must not consume a sequence
            // number, or the gap would read as a deletion.
            chain_id: None,
            chain_seq: None,
            prev_hash: None,
            row_hash: None,
            chain_version: None,
            // Synthetic row, built outside a request scope.
            turn_id: None,
            turn_seq: None,
            // Decision record: this synthetic row records no policy
            // decision of its own.
            argument_policy_id: None,
            guardrail_config_id: None,
            guardrail_check: None,
            guardrail_action: None,
            guardrails_skipped: None,
            guardrails_degraded: None,
            autonomy_level: None,
            autonomy_provenance: None,
            consumed_approval_request_id: None,
            upstream_request_id: None,
            // Synthetic row; no quota decision was taken.
            pre_action_state: None,
            request_content_hash: None,
            response_content_hash: None,
            trace_id: None,
            span_id: None,
            server_version: None,
            tool_definition_hash: None,
            deciding_model: None,
            record_id: None,
            a2a_task_state: None,
            agent_impl_name: None,
            agent_impl_version: None,
            agent_impl_build: None,
            agent_impl_source: None,
            client_label: None,
            custom_metadata: None,
            tags: None,
        };
        log.residency_class = residency_class.map(str::to_owned);
        log.http_status_code = status;
        log.gateway_user_id = user_id.map(str::to_owned);
        log
    }

    fn profile_with(items: &[&str]) -> ComplianceProfile {
        ComplianceProfile {
            frameworks: items.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn from_log_state_iso_42001_always_fires() {
        // ISO 42001 is the "always on" baseline. Even a quiet
        // call gets the tag when the framework is declared.
        let p = profile_with(&["iso_42001"]);
        let rg = RgComplianceFields::default();
        let log = make_log_with(None, Some(200), Some("u-1"));
        let tags = derive_tags_from_log_state(&p, &rg, &log).expect("Some");
        assert_eq!(tags, vec!["iso_42001"]);
    }

    #[test]
    fn from_log_state_residency_block_lands_soc2_cc67() {
        // Residency-blocked audit rows have
        // log.residency_class = "blocked" stamped by the LLM/MCP
        // route. The compliance engine reads that.
        let p = profile_with(&["soc2"]);
        let rg = RgComplianceFields::default();
        let log = make_log_with(Some("blocked"), Some(403), Some("u-1"));
        let tags = derive_tags_from_log_state(&p, &rg, &log).expect("Some");
        assert!(tags.contains(&TAG_SOC2_RESIDENCY_BLOCK.to_string()));
    }

    #[test]
    fn from_log_state_rate_limit_lands_soc2_cc71() {
        let p = profile_with(&["soc2"]);
        let rg = RgComplianceFields::default();
        let log = make_log_with(None, Some(429), Some("u-1"));
        let tags = derive_tags_from_log_state(&p, &rg, &log).expect("Some");
        assert!(tags.contains(&TAG_SOC2_RATE_LIMIT_HIT.to_string()));
    }

    #[test]
    fn from_log_state_eu_ai_act_emits_per_rg_tier() {
        let p = profile_with(&["eu_ai_act"]);
        let rg = RgComplianceFields {
            eu_ai_act_risk_tier: Some("high".to_string()),
            phi_resource: false,
        };
        let log = make_log_with(None, Some(200), Some("u-1"));
        let tags = derive_tags_from_log_state(&p, &rg, &log).expect("Some");
        assert!(tags.contains(&"eu_ai_act.high".to_string()));
    }

    #[test]
    fn from_log_state_hipaa_rg_marked_fires_when_phi_resource_true() {
        let p = profile_with(&["hipaa"]);
        let rg = RgComplianceFields {
            eu_ai_act_risk_tier: None,
            phi_resource: true,
        };
        let log = make_log_with(None, Some(200), Some("u-1"));
        let tags = derive_tags_from_log_state(&p, &rg, &log).expect("Some");
        assert!(tags.contains(&TAG_HIPAA_RG_MARKED.to_string()));
    }

    #[test]
    fn from_log_state_no_profile_returns_none() {
        let p = profile_with(&[]);
        let rg = RgComplianceFields::default();
        let log = make_log_with(Some("blocked"), Some(403), Some("u-1"));
        assert!(derive_tags_from_log_state(&p, &rg, &log).is_none());
    }

    // -----------------------------------------------------------------
    // Orchestrator outcome flags drive SOC 2 + GDPR + HIPAA
    // -----------------------------------------------------------------

    #[test]
    fn from_log_state_guardrail_blocked_fires_cc72() {
        let p = profile_with(&["soc2"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(403), Some("u-1"));
        log.guardrail_blocked = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(tags.contains(&TAG_SOC2_GUARDRAIL_BLOCK.to_string()));
    }

    #[test]
    fn from_log_state_argument_policy_denied_fires_cc66() {
        let p = profile_with(&["soc2"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(403), Some("u-1"));
        log.argument_policy_denied = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(tags.contains(&TAG_SOC2_POLICY_DENY.to_string()));
    }

    #[test]
    fn from_log_state_is_auth_event_fires_cc61() {
        let p = profile_with(&["soc2"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(200), Some("u-1"));
        log.is_auth_event = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(tags.contains(&TAG_SOC2_AUTH_EVENT.to_string()));
    }

    #[test]
    fn from_log_state_pii_with_user_fires_gdpr() {
        let p = profile_with(&["gdpr_art_30"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(200), Some("u-1"));
        log.pii_detected = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(tags.contains(&TAG_GDPR_ART_30.to_string()));
    }

    #[test]
    fn from_log_state_pii_without_identifiable_subject_does_not_fire_gdpr() {
        // Anonymous PII still triggers the detector but doesn't
        // qualify for Article 30 (no data subject to record).
        let p = profile_with(&["gdpr_art_30"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(200), None);
        log.pii_detected = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(!tags.contains(&TAG_GDPR_ART_30.to_string()));
    }

    #[test]
    fn from_log_state_pii_fires_hipaa_phi_when_declared() {
        // The conservative reading: any PII on a HIPAA tenant is
        // tagged as PHI. False positives go to the auditor's
        // review queue; false negatives are unacceptable.
        let p = profile_with(&["hipaa"]);
        let rg = RgComplianceFields::default();
        let mut log = make_log_with(None, Some(200), Some("u-1"));
        log.pii_detected = true;
        let tags = derive_tags_from_log_state(&p, &rg, &log).unwrap();
        assert!(tags.contains(&TAG_HIPAA_PHI.to_string()));
    }
}
