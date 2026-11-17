// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! AI System attribution.
//!
//! An **AI System** is a Resource Group with `group_type='ai_system'`.
//! It is the entity the platform runs a lifecycle
//! for: runs are attributed to an AI System, baselines are computed per AI
//! System, drift is detected per AI System, and enforcement responses act
//! on an AI System. This module answers the one question every one of
//! those depends on:
//!
//! > *Which AI System does this call belong to?*
//!
//! ## Resolution order
//!
//! Most-specific first:
//!
//!   1. explicit `X-Brutor-AI-System` header (validated — see below)
//!   2. the acting agent identity's AI System membership
//!   3. the API key's bound resource group
//!   4. the resource group that authorized the call
//!
//! Each candidate is a *resource group id*, which is then walked up the
//! hierarchy to the nearest ancestor (self included) that is an AI System.
//! That walk is what lets an operator organise `Acme > Payments > Refund
//! Agent` and have a call authorized against a leaf group still attribute
//! to the AI System above it.
//!
//! A call that resolves to nothing is **unattributed**, not an error. Those
//! surface in the fleet board's "Unattributed runs" view, which doubles as
//! the adoption funnel: it is exactly the list of traffic a customer has
//! not yet organised into AI Systems.
//!
//! ## Why the header is validated
//!
//! Attribution feeds an audit trail and automatic
//! enforcement. An unvalidated caller-supplied header would let any client
//! attribute its traffic to someone else's AI System — poisoning another
//! team's baselines and, worse, letting a caller dodge a drift response by
//! renaming itself. So a header claim is honoured only when the claimed
//! system is the authorizing group itself or an ancestor of it: the caller
//! may name the system it is *already inside*, never a sibling's.
//!
//! Rejected claims are not silently dropped — they are returned in
//! [`AiSystemAttribution::rejected_claim`] so the caller's attempt is
//! visible rather than invisible.
//!
//! ## Case trap
//!
//! The Postgres `grouptype` enum stores SQLAlchemy member *names*, so raw
//! SQL sees `'AI_SYSTEM'` while the control-plane JSON API emits the
//! lowercase *value* `'ai_system'`. Every comparison here is
//! case-insensitive so neither convention can silently break attribution.

use sqlx::PgPool;

/// Header a client may use to name the AI System a call belongs to.
pub const AI_SYSTEM_HEADER: &str = "X-Brutor-AI-System";

/// How deep the ancestry walk goes before giving up. Matches the bound
/// used by `ResourceGroupCache::load_effective_group`, so attribution and
/// the effective-group materialisation agree about what "the chain" is.
const MAX_ANCESTRY_DEPTH: i32 = 10;

/// Which rung of the resolution ladder produced the attribution.
///
/// Recorded so an operator debugging a mis-attributed run can see *why* it
/// landed where it did without re-deriving the ladder by hand.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AiSystemSource {
    /// Explicit, validated `X-Brutor-AI-System` header.
    Header,
    /// The acting agent identity's resource-group membership.
    AgentIdentity,
    /// The resource group the API key is bound to.
    ApiKey,
    /// The resource group that authorized the call.
    AuthorizingGroup,
}

impl AiSystemSource {
    /// Stable wire string — pinned by test, and stored in the
    /// `ai_system_runs` ledger.
    pub fn as_str(&self) -> &'static str {
        match self {
            AiSystemSource::Header => "header",
            AiSystemSource::AgentIdentity => "agent_identity",
            AiSystemSource::ApiKey => "api_key",
            AiSystemSource::AuthorizingGroup => "authorizing_group",
        }
    }
}

/// The outcome of attribution for one call.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AiSystemAttribution {
    /// Resource-group id of the AI System, or `None` when unattributed.
    pub ai_system_id: Option<String>,
    /// The system's kind (`agent`, `integration`, …) when attributed.
    pub system_kind: Option<String>,
    /// Which rung resolved it.
    pub source: Option<AiSystemSource>,
    /// A header claim that was refused, kept for audit. `Some` here with
    /// an `ai_system_id` from a lower rung means "the caller asked for X,
    /// we attributed to Y".
    pub rejected_claim: Option<String>,
}

impl AiSystemAttribution {
    /// True when the call could not be tied to any AI System.
    pub fn is_unattributed(&self) -> bool {
        self.ai_system_id.is_none()
    }

    fn resolved(
        id: String,
        kind: Option<String>,
        source: AiSystemSource,
        rejected: Option<String>,
    ) -> Self {
        Self {
            ai_system_id: Some(id),
            system_kind: kind,
            source: Some(source),
            rejected_claim: rejected,
        }
    }
}

/// Everything attribution needs from the request, decoupled from route
/// plumbing so it can be unit-tested without constructing an HTTP request.
#[derive(Debug, Clone, Default)]
pub struct AttributionInputs<'a> {
    /// Raw `X-Brutor-AI-System` header value, if the client sent one.
    pub header_claim: Option<&'a str>,
    /// Resource group the acting agent identity belongs to.
    pub agent_group_id: Option<&'a str>,
    /// Resource group the API key is bound to.
    pub api_key_group_id: Option<&'a str>,
    /// Resource group that authorized the call.
    pub authorizing_group_id: Option<&'a str>,
}

/// Nearest AI System at or above `group_id`, as `(id, system_kind)`.
///
/// One recursive CTE rather than a walk of up-to-ten round trips: this sits
/// on the request path, and ten sequential queries per call to answer a
/// question about static topology would be indefensible.
///
/// A database error is returned, never read as "no AI System": that
/// would seal an unattributed record or misfile a run.
pub async fn nearest_ai_system(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
) -> Result<Option<(String, Option<String>)>, sqlx::Error> {
    let row: Option<(String, Option<String>)> = sqlx::query_as(
        r#"
        WITH RECURSIVE chain AS (
            SELECT id,
                   parent_group_id,
                   group_type::text AS gt,
                   system_kind,
                   0 AS depth
              FROM resource_groups
             WHERE id = $1 AND tenant_id = $2
            UNION ALL
            SELECT rg.id,
                   rg.parent_group_id,
                   rg.group_type::text,
                   rg.system_kind,
                   c.depth + 1
              FROM resource_groups rg
              JOIN chain c ON rg.id = c.parent_group_id
             WHERE rg.tenant_id = $2 AND c.depth < $3
        )
        SELECT id, system_kind
          FROM chain
         WHERE UPPER(gt) = 'AI_SYSTEM'
         ORDER BY depth ASC
         LIMIT 1
        "#,
    )
    .bind(group_id)
    .bind(tenant_id)
    .bind(MAX_ANCESTRY_DEPTH)
    .fetch_optional(db)
    .await?;

    Ok(row)
}

/// Nearest AI System for MANY groups at once — one query, not one per group.
///
/// `finalize_run` resolves the "via" list by asking this
/// question for every group a run touched, and the sweeper finalizes up to
/// `SWEEP_BATCH_SIZE` runs per tick. Asking it one group at a time turned a
/// fan-out chain into hundreds of sequential round trips inside a 30 s
/// window, at exactly the moment the sweeper is already doing the heaviest
/// aggregate query in the process.
///
/// Same semantics as [`nearest_ai_system`], per group: the chain climbs to
/// `MAX_ANCESTRY_DEPTH` and the SHALLOWEST AI System wins. Groups with no AI
/// System above them are simply absent from the returned map, which is the
/// map-shaped spelling of `None`.
pub async fn nearest_ai_systems(
    db: &PgPool,
    tenant_id: &str,
    group_ids: &[String],
) -> Result<std::collections::HashMap<String, (String, Option<String>)>, sqlx::Error> {
    let mut out = std::collections::HashMap::new();
    if group_ids.is_empty() {
        return Ok(out);
    }
    // `origin` threads each chain back to the group it started from, so one
    // recursive walk answers every group's question independently. DISTINCT
    // ON + ORDER BY depth is the set-based form of the single-row query's
    // `ORDER BY depth ASC LIMIT 1`.
    let rows: Vec<(String, String, Option<String>)> = sqlx::query_as(
        r#"
        WITH RECURSIVE chain AS (
            SELECT id AS origin,
                   id,
                   parent_group_id,
                   group_type::text AS gt,
                   system_kind,
                   0 AS depth
              FROM resource_groups
             WHERE id = ANY($1) AND tenant_id = $2
            UNION ALL
            SELECT c.origin,
                   rg.id,
                   rg.parent_group_id,
                   rg.group_type::text,
                   rg.system_kind,
                   c.depth + 1
              FROM resource_groups rg
              JOIN chain c ON rg.id = c.parent_group_id
             WHERE rg.tenant_id = $2 AND c.depth < $3
        )
        SELECT DISTINCT ON (origin) origin, id, system_kind
          FROM chain
         WHERE UPPER(gt) = 'AI_SYSTEM'
         ORDER BY origin, depth ASC
        "#,
    )
    .bind(group_ids)
    .bind(tenant_id)
    .bind(MAX_ANCESTRY_DEPTH)
    .fetch_all(db)
    .await?;

    for (origin, system_id, kind) in rows {
        out.insert(origin, (system_id, kind));
    }
    Ok(out)
}

/// True when `candidate_id` is `descendant_id` or one of its ancestors.
///
/// This is the containment test behind header validation: a caller may
/// name an AI System it is already inside, never a sibling's.
pub async fn is_self_or_ancestor(
    db: &PgPool,
    tenant_id: &str,
    candidate_id: &str,
    descendant_id: &str,
) -> Result<bool, sqlx::Error> {
    if candidate_id == descendant_id {
        return Ok(true);
    }
    let hit: Option<(i64,)> = sqlx::query_as(
        r#"
        WITH RECURSIVE chain AS (
            SELECT id, parent_group_id, 0 AS depth
              FROM resource_groups
             WHERE id = $1 AND tenant_id = $2
            UNION ALL
            SELECT rg.id, rg.parent_group_id, c.depth + 1
              FROM resource_groups rg
              JOIN chain c ON rg.id = c.parent_group_id
             WHERE rg.tenant_id = $2 AND c.depth < $4
        )
        SELECT COUNT(*) FROM chain WHERE id = $3
        "#,
    )
    .bind(descendant_id)
    .bind(tenant_id)
    .bind(candidate_id)
    .bind(MAX_ANCESTRY_DEPTH)
    .fetch_optional(db)
    .await?;

    Ok(matches!(hit, Some((n,)) if n > 0))
}

/// Look up a group by id and return it if it is an active AI System.
async fn load_ai_system(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
) -> Result<Option<(String, Option<String>)>, sqlx::Error> {
    let row: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT id, system_kind FROM resource_groups \
         WHERE id = $1 AND tenant_id = $2 AND is_active = true \
           AND UPPER(group_type::text) = 'AI_SYSTEM'",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?;

    Ok(row)
}

/// Resolve the AI System for a call.
///
/// An unresolvable call is unattributed, which is a first class state. A
/// database error is not: it is returned, because reading it as
/// "unattributed", or a valid header claim as a rejected one, would record a
/// fact that is not true.
pub async fn resolve_ai_system(
    db: &PgPool,
    tenant_id: &str,
    inputs: &AttributionInputs<'_>,
) -> Result<AiSystemAttribution, sqlx::Error> {
    let mut rejected_claim: Option<String> = None;

    // --- rung 1: explicit header claim -------------------------------------
    if let Some(claim) = inputs.header_claim.map(str::trim).filter(|c| !c.is_empty()) {
        // The claim must name a real, active AI System in this tenant …
        match load_ai_system(db, tenant_id, claim).await? {
            Some((id, kind)) => {
                // … and the caller must already be inside it. Without an
                // authorizing group there is nothing to contain the claim
                // against, so it cannot be honoured.
                let containment_anchor = inputs
                    .authorizing_group_id
                    .or(inputs.agent_group_id)
                    .or(inputs.api_key_group_id);

                let permitted = match containment_anchor {
                    Some(anchor) => is_self_or_ancestor(db, tenant_id, &id, anchor).await?,
                    None => false,
                };

                if permitted {
                    return Ok(AiSystemAttribution::resolved(
                        id,
                        kind,
                        AiSystemSource::Header,
                        None,
                    ));
                }
                rejected_claim = Some(claim.to_string());
            }
            None => {
                // Unknown id, wrong tenant, inactive, or not an AI System.
                rejected_claim = Some(claim.to_string());
            }
        }
    }

    // --- rungs 2-4: derive from the call's own identity ---------------------
    let ladder = [
        (inputs.agent_group_id, AiSystemSource::AgentIdentity),
        (inputs.api_key_group_id, AiSystemSource::ApiKey),
        (
            inputs.authorizing_group_id,
            AiSystemSource::AuthorizingGroup,
        ),
    ];

    for (group_id, source) in ladder {
        let Some(group_id) = group_id.map(str::trim).filter(|g| !g.is_empty()) else {
            continue;
        };
        if let Some((id, kind)) = nearest_ai_system(db, tenant_id, group_id).await? {
            return Ok(AiSystemAttribution::resolved(
                id,
                kind,
                source,
                rejected_claim,
            ));
        }
    }

    Ok(AiSystemAttribution {
        ai_system_id: None,
        system_kind: None,
        source: None,
        rejected_claim,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_wire_strings_are_stable() {
        // Pinned: these land in the run ledger and in audit output,
        // so a rename is a wire-contract change, not a refactor.
        assert_eq!(AiSystemSource::Header.as_str(), "header");
        assert_eq!(AiSystemSource::AgentIdentity.as_str(), "agent_identity");
        assert_eq!(AiSystemSource::ApiKey.as_str(), "api_key");
        assert_eq!(
            AiSystemSource::AuthorizingGroup.as_str(),
            "authorizing_group"
        );
    }

    #[test]
    fn header_constant_matches_rfc() {
        assert_eq!(AI_SYSTEM_HEADER, "X-Brutor-AI-System");
    }

    #[test]
    fn default_attribution_is_unattributed() {
        let a = AiSystemAttribution::default();
        assert!(a.is_unattributed());
        assert!(a.source.is_none());
        assert!(a.rejected_claim.is_none());
    }

    #[test]
    fn resolved_attribution_is_not_unattributed() {
        let a = AiSystemAttribution::resolved(
            "group-1".into(),
            Some("agent".into()),
            AiSystemSource::ApiKey,
            None,
        );
        assert!(!a.is_unattributed());
        assert_eq!(a.ai_system_id.as_deref(), Some("group-1"));
        assert_eq!(a.system_kind.as_deref(), Some("agent"));
        assert_eq!(a.source, Some(AiSystemSource::ApiKey));
    }

    #[test]
    fn rejected_claim_survives_fallback_resolution() {
        // The shape the resolver produces when a header claim is refused but
        // a lower rung still attributes: both facts must be visible.
        let a = AiSystemAttribution::resolved(
            "group-real".into(),
            Some("integration".into()),
            AiSystemSource::AuthorizingGroup,
            Some("group-claimed".into()),
        );
        assert_eq!(a.ai_system_id.as_deref(), Some("group-real"));
        assert_eq!(a.rejected_claim.as_deref(), Some("group-claimed"));
    }

    #[test]
    fn ancestry_depth_matches_group_cache_bound() {
        // ResourceGroupCache::load_effective_group walks `for _ in 0..10`.
        // If these diverge, a call can attribute to an AI System whose
        // limits the cache never merged (or vice versa).
        assert_eq!(MAX_ANCESTRY_DEPTH, 10);
    }
}
