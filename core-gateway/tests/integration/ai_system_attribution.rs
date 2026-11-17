// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! AI System attribution against a real Postgres.
//!
//! The in-source unit tests in `services::ai_system` cover the data shapes.
//! Everything that actually matters lives in SQL — the recursive ancestry
//! walk, the case-insensitive `grouptype` comparison, the containment check
//! behind header validation — and none of it can be exercised without a
//! database. That is what this file is for.
//!
//! The case trap is worth stating once more because it is the bug this
//! suite exists to prevent: the Postgres `grouptype` enum stores SQLAlchemy
//! member *names*, so a row written by the control plane reads back as
//! `'AI_SYSTEM'`, while every JSON API and the admin UI use the lowercase
//! value `'ai_system'`. A resolver that compared against the lowercase form
//! would attribute *nothing*, silently, forever.

use sqlx::PgPool;

use brutor_gateway_core::services::ai_system::{
    AiSystemSource, AttributionInputs, is_self_or_ancestor, nearest_ai_system, resolve_ai_system,
};

use super::common;

/// Insert a resource group with an explicit type, kind and parent.
///
/// Deliberately not reusing `common::seed_resource_group` — that helper
/// hard-codes a flat, typeless group, and every case here turns on type,
/// kind or placement in a hierarchy.
async fn seed_group(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    parent: Option<&str>,
    group_type: Option<&str>,
    system_kind: Option<&str>,
    level: i32,
) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, parent_group_id, hierarchy_level, \
         inherit_from_parent, name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, $3, $4, true, $1, true, false, \
                 CAST($5 AS public.grouptype), $6) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(parent)
    .bind(level)
    .bind(group_type)
    .bind(system_kind)
    .execute(pool)
    .await
    .expect("seed_group");
}

async fn deactivate(pool: &PgPool, id: &str) {
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = $1")
        .bind(id)
        .execute(pool)
        .await
        .expect("deactivate");
}

/// `Acme (org) > Payments (ai_system, integration) > Refund (team)`
/// plus an unrelated `Marketing (ai_system, assistant)` sibling tree.
async fn seed_fleet(pool: &PgPool, tenant: &str) {
    common::seed_tenant(pool, tenant).await;
    seed_group(pool, "acme", tenant, None, Some("ORGANIZATION"), None, 0).await;
    seed_group(
        pool,
        "payments",
        tenant,
        Some("acme"),
        Some("AI_SYSTEM"),
        Some("integration"),
        1,
    )
    .await;
    seed_group(
        pool,
        "refund",
        tenant,
        Some("payments"),
        Some("TEAM"),
        None,
        2,
    )
    .await;
    seed_group(
        pool,
        "marketing",
        tenant,
        Some("acme"),
        Some("AI_SYSTEM"),
        Some("assistant"),
        1,
    )
    .await;
}

// ---------------------------------------------------------------------------
// nearest_ai_system
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn nearest_ai_system_finds_self(pool: PgPool) {
    let t = "t-ais-self";
    seed_fleet(&pool, t).await;

    let found = nearest_ai_system(&pool, t, "payments").await.unwrap();
    assert_eq!(
        found,
        Some(("payments".to_string(), Some("integration".to_string()))),
        "a group that IS an AI System must resolve to itself"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn nearest_ai_system_walks_up_to_ancestor(pool: PgPool) {
    let t = "t-ais-walk";
    seed_fleet(&pool, t).await;

    // This is the case that makes the whole model usable: a call authorized
    // against a leaf team still attributes to the AI System above it.
    let found = nearest_ai_system(&pool, t, "refund").await.unwrap();
    assert_eq!(
        found,
        Some(("payments".to_string(), Some("integration".to_string())))
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn nearest_ai_system_returns_none_when_no_ancestor_qualifies(pool: PgPool) {
    let t = "t-ais-none";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, "org", t, None, Some("ORGANIZATION"), None, 0).await;
    seed_group(&pool, "team", t, Some("org"), Some("TEAM"), None, 1).await;

    assert_eq!(
        nearest_ai_system(&pool, t, "team").await.unwrap(),
        None,
        "unattributed is a valid outcome, not an error"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn nearest_ai_system_prefers_the_closest_ancestor(pool: PgPool) {
    let t = "t-ais-closest";
    common::seed_tenant(&pool, t).await;
    seed_group(
        &pool,
        "outer",
        t,
        None,
        Some("AI_SYSTEM"),
        Some("service"),
        0,
    )
    .await;
    seed_group(
        &pool,
        "inner",
        t,
        Some("outer"),
        Some("AI_SYSTEM"),
        Some("agent"),
        1,
    )
    .await;
    seed_group(&pool, "leaf", t, Some("inner"), Some("TEAM"), None, 2).await;

    // Nested AI Systems are legal (a shared service inside a larger system).
    // The nearest one wins, or attribution would drift upward and every run
    // would land on the outermost box.
    assert_eq!(
        nearest_ai_system(&pool, t, "leaf").await.unwrap(),
        Some(("inner".to_string(), Some("agent".to_string())))
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn nearest_ai_system_is_tenant_scoped(pool: PgPool) {
    let t1 = "t-ais-iso-1";
    let t2 = "t-ais-iso-2";
    seed_fleet(&pool, t1).await;
    common::seed_tenant(&pool, t2).await;

    assert_eq!(
        nearest_ai_system(&pool, t2, "payments").await.unwrap(),
        None,
        "another tenant's AI System must be invisible"
    );
}

// ---------------------------------------------------------------------------
// is_self_or_ancestor — the containment check behind header validation
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn containment_accepts_self_and_ancestors(pool: PgPool) {
    let t = "t-ais-contain";
    seed_fleet(&pool, t).await;

    assert!(
        is_self_or_ancestor(&pool, t, "refund", "refund")
            .await
            .unwrap()
    );
    assert!(
        is_self_or_ancestor(&pool, t, "payments", "refund")
            .await
            .unwrap()
    );
    assert!(
        is_self_or_ancestor(&pool, t, "acme", "refund")
            .await
            .unwrap()
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn containment_rejects_siblings_and_descendants(pool: PgPool) {
    let t = "t-ais-contain-neg";
    seed_fleet(&pool, t).await;

    assert!(
        !is_self_or_ancestor(&pool, t, "marketing", "refund")
            .await
            .unwrap(),
        "a sibling AI System must not contain refund — this is the spoofing case"
    );
    assert!(
        !is_self_or_ancestor(&pool, t, "refund", "payments")
            .await
            .unwrap(),
        "containment is upward-only; a child does not contain its parent"
    );
}

// ---------------------------------------------------------------------------
// resolve_ai_system — the ladder
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn resolves_from_authorizing_group(pool: PgPool) {
    let t = "t-ais-authz";
    seed_fleet(&pool, t).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("payments"));
    assert_eq!(out.system_kind.as_deref(), Some("integration"));
    assert_eq!(out.source, Some(AiSystemSource::AuthorizingGroup));
    assert!(out.rejected_claim.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn agent_identity_outranks_api_key_and_authorizing_group(pool: PgPool) {
    let t = "t-ais-ladder";
    seed_fleet(&pool, t).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            agent_group_id: Some("marketing"),
            api_key_group_id: Some("refund"),
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("marketing"));
    assert_eq!(out.source, Some(AiSystemSource::AgentIdentity));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn ladder_skips_rungs_that_resolve_to_nothing(pool: PgPool) {
    let t = "t-ais-skip";
    seed_fleet(&pool, t).await;
    // A group outside any AI System — rung 2 must fall through, not abort.
    seed_group(&pool, "orphan", t, Some("acme"), Some("TEAM"), None, 1).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            agent_group_id: Some("orphan"),
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("payments"));
    assert_eq!(out.source, Some(AiSystemSource::AuthorizingGroup));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn unattributed_when_nothing_resolves(pool: PgPool) {
    let t = "t-ais-unattr";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, "lonely", t, None, Some("TEAM"), None, 0).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            authorizing_group_id: Some("lonely"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert!(out.is_unattributed());
    assert!(out.source.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_inputs_at_all_is_unattributed(pool: PgPool) {
    let t = "t-ais-empty";
    seed_fleet(&pool, t).await;

    let out = resolve_ai_system(&pool, t, &AttributionInputs::default())
        .await
        .unwrap();
    assert!(out.is_unattributed());
}

// ---------------------------------------------------------------------------
// Header claims
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn valid_header_claim_wins(pool: PgPool) {
    let t = "t-ais-hdr-ok";
    common::seed_tenant(&pool, t).await;
    seed_group(
        &pool,
        "outer",
        t,
        None,
        Some("AI_SYSTEM"),
        Some("service"),
        0,
    )
    .await;
    seed_group(
        &pool,
        "inner",
        t,
        Some("outer"),
        Some("AI_SYSTEM"),
        Some("agent"),
        1,
    )
    .await;

    // Caller authorized against `inner` names the enclosing `outer`, which
    // it is legitimately inside.
    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("outer"),
            authorizing_group_id: Some("inner"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("outer"));
    assert_eq!(out.source, Some(AiSystemSource::Header));
    assert!(out.rejected_claim.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn header_claiming_a_sibling_system_is_refused_and_recorded(pool: PgPool) {
    let t = "t-ais-hdr-spoof";
    seed_fleet(&pool, t).await;

    // The spoofing attempt: a caller inside Payments claims Marketing, to
    // poison another team's baselines or dodge a drift response.
    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("marketing"),
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(
        out.ai_system_id.as_deref(),
        Some("payments"),
        "must fall back to the honest attribution"
    );
    assert_eq!(out.source, Some(AiSystemSource::AuthorizingGroup));
    assert_eq!(
        out.rejected_claim.as_deref(),
        Some("marketing"),
        "the refused claim must stay visible, not vanish"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn header_naming_a_non_ai_system_group_is_refused(pool: PgPool) {
    let t = "t-ais-hdr-nonsys";
    seed_fleet(&pool, t).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("acme"), // an organization, not an AI System
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("payments"));
    assert_eq!(out.rejected_claim.as_deref(), Some("acme"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn header_from_another_tenant_is_refused(pool: PgPool) {
    let t1 = "t-ais-hdr-x1";
    let t2 = "t-ais-hdr-x2";
    seed_fleet(&pool, t1).await;
    common::seed_tenant(&pool, t2).await;
    seed_group(
        &pool,
        "t2sys",
        t2,
        None,
        Some("AI_SYSTEM"),
        Some("agent"),
        0,
    )
    .await;

    let out = resolve_ai_system(
        &pool,
        t1,
        &AttributionInputs {
            header_claim: Some("t2sys"),
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("payments"));
    assert_eq!(out.rejected_claim.as_deref(), Some("t2sys"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn header_without_any_containment_anchor_is_refused(pool: PgPool) {
    let t = "t-ais-hdr-noanchor";
    seed_fleet(&pool, t).await;

    // Nothing to contain the claim against — an unauthenticated-ish shape.
    // Honouring it here would make attribution purely caller-asserted.
    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("payments"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert!(out.is_unattributed());
    assert_eq!(out.rejected_claim.as_deref(), Some("payments"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn blank_header_is_ignored_not_rejected(pool: PgPool) {
    let t = "t-ais-hdr-blank";
    seed_fleet(&pool, t).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("   "),
            authorizing_group_id: Some("refund"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("payments"));
    assert!(
        out.rejected_claim.is_none(),
        "an empty header is absence, not an attempted claim"
    );
}

// ---------------------------------------------------------------------------
// Lifecycle edges
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn inactive_ai_system_is_not_claimable_by_header(pool: PgPool) {
    let t = "t-ais-inactive-hdr";
    common::seed_tenant(&pool, t).await;
    seed_group(
        &pool,
        "outer",
        t,
        None,
        Some("AI_SYSTEM"),
        Some("service"),
        0,
    )
    .await;
    seed_group(
        &pool,
        "inner",
        t,
        Some("outer"),
        Some("AI_SYSTEM"),
        Some("agent"),
        1,
    )
    .await;
    deactivate(&pool, "outer").await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            header_claim: Some("outer"),
            authorizing_group_id: Some("inner"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.rejected_claim.as_deref(), Some("outer"));
    assert_eq!(out.ai_system_id.as_deref(), Some("inner"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn ai_system_without_a_kind_still_attributes(pool: PgPool) {
    // Defence in depth: stored rows have a `system_kind`, and the model's flush
    // hook defaults it. If a row somehow lacks one anyway, attribution must
    // degrade to "kind unknown" rather than dropping the run on the floor.
    let t = "t-ais-nokind";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, "sys", t, None, Some("AI_SYSTEM"), None, 0).await;

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            authorizing_group_id: Some("sys"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("sys"));
    assert_eq!(out.system_kind, None);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn deep_hierarchy_within_bound_resolves(pool: PgPool) {
    let t = "t-ais-deep";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, "d0", t, None, Some("AI_SYSTEM"), Some("workflow"), 0).await;
    // 8 nested plain groups below the system — inside the depth bound of 10.
    let mut parent = "d0".to_string();
    for i in 1..=8 {
        let id = format!("d{i}");
        seed_group(&pool, &id, t, Some(&parent), Some("TEAM"), None, i).await;
        parent = id;
    }

    let out = resolve_ai_system(
        &pool,
        t,
        &AttributionInputs {
            authorizing_group_id: Some("d8"),
            ..Default::default()
        },
    )
    .await
    .unwrap();

    assert_eq!(out.ai_system_id.as_deref(), Some("d0"));
}
