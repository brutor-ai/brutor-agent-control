// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// DB-integration tests for the per-agent runtime authorization engine:
// `agent_authz::decide_for_agent` loads an agent's default_deny + live grants
// from Postgres and decides (combined strictest-wins with the RG decision).

use brutor_gateway_core::services::agent_authz::{Decision, decide_for_agent};
use sqlx::PgPool;

use crate::common;

const TENANT: &str = "t-agent-authz";

async fn seed_agent(pool: &PgPool, id: &str, default_deny: bool, enabled: bool, status: &str) {
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ($1, $2, $3, 'manual', $4, $5, $6) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(TENANT)
    .bind(id)
    .bind(enabled)
    .bind(status)
    .bind(default_deny)
    .execute(pool)
    .await
    .unwrap();
}

async fn seed_grant(
    pool: &PgPool,
    id: &str,
    agent_id: &str,
    action_type: &str,
    target: &str,
    effect: &str,
) {
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ($1, $2, $3, $4, $5, $6, true) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(TENANT)
    .bind(agent_id)
    .bind(action_type)
    .bind(target)
    .bind(effect)
    .execute(pool)
    .await
    .unwrap();
}

async fn seed_grant_with_constraints(
    pool: &PgPool,
    id: &str,
    agent_id: &str,
    action_type: &str,
    target: &str,
    effect: &str,
    constraints: serde_json::Value,
) {
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled, constraints) \
         VALUES ($1, $2, $3, $4, $5, $6, true, $7) ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(TENANT)
    .bind(agent_id)
    .bind(action_type)
    .bind(target)
    .bind(effect)
    .bind(constraints)
    .execute(pool)
    .await
    .unwrap();
}

/// An agent actor whose identity the gate cannot
/// read in the tenant it acts in is denied, never "no gate": an id that is
/// no row, and an agent of another tenant (a shared key bound to an agent,
/// acting for a listed tenant, authenticates as the agent of its own
/// tenant). Not softened by dry-run: there is no row to read a mode from.
/// (Was `unknown_agent_returns_none`, which pinned `Ok(None)`.)
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_with_no_identity_in_the_acting_tenant_is_denied(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    common::seed_tenant(&pool, "tenant-f481-elsewhere").await;
    // Not default-deny and in dry-run: in its own tenant it is allowed.
    seed_agent(&pool, "agent-f481-home", false, true, "active").await;
    sqlx::query(
        "UPDATE agent_identities SET enforcement_mode = 'dry_run' WHERE id = 'agent-f481-home'",
    )
    .execute(&pool)
    .await
    .unwrap();
    let decide = |tenant: &'static str, agent: &'static str| {
        let pool = pool.clone();
        async move {
            decide_for_agent(
                &pool,
                tenant,
                agent,
                "llm_call",
                "gpt-5.2",
                None,
                None,
                None,
                &[],
            )
            .await
            .unwrap()
        }
    };
    let home = decide(TENANT, "agent-f481-home").await;
    assert_eq!(home.decision, Decision::Allow);

    for (tenant, agent) in [
        (TENANT, "agent-nope"),
        ("tenant-f481-elsewhere", "agent-f481-home"),
    ] {
        let out = decide(tenant, agent).await;
        assert_eq!(out.decision, Decision::Deny, "{tenant}/{agent}");
        assert!(!out.dry_run, "{tenant}/{agent}: enforced");
        assert_eq!(out.matched_grant_id, None);
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn default_deny_with_no_grant(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-dd", true, true, "active").await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-dd",
        "llm_call",
        "gpt-5.2",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(out.decision, Decision::Deny);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn allow_grant_permits(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-a", true, true, "active").await;
    seed_grant(&pool, "g-allow", "agent-a", "llm_call", "gpt-*", "allow").await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-a",
        "llm_call",
        "gpt-5.2",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(out.decision, Decision::Allow);
    assert_eq!(out.matched_grant_id.as_deref(), Some("g-allow"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn deny_grant_wins_over_rg_allow(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-d", false, true, "active").await;
    seed_grant(&pool, "g-deny", "agent-d", "mcp_tool", "shell.*", "deny").await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-d",
        "mcp_tool",
        "shell.exec",
        Some(Decision::Allow),
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(out.decision, Decision::Deny);
    assert_eq!(out.matched_grant_id.as_deref(), Some("g-deny"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn rg_deny_overrides_agent_allow(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-r", true, true, "active").await;
    seed_grant(&pool, "g-a2", "agent-r", "llm_call", "*", "allow").await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-r",
        "llm_call",
        "gpt-5.2",
        Some(Decision::Deny),
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(out.decision, Decision::Deny);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn action_types_are_scoped_per_surface(pool: PgPool) {
    // A grant for one action type must not authorize another, and each
    // surface's action_type resolves its own grants. Covers mcp_tool / a2a_call
    // / skill_exec alongside llm_call so every enforcement hook's action path
    // has DB-level regression coverage.
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-multi", true, true, "active").await;
    seed_grant(
        &pool,
        "g-a2a",
        "agent-multi",
        "a2a_call",
        "card-billing",
        "allow",
    )
    .await;
    seed_grant(&pool, "g-skill", "agent-multi", "skill_exec", "*", "allow").await;

    // a2a_call to the granted card → allow
    let a2a = decide_for_agent(
        &pool,
        TENANT,
        "agent-multi",
        "a2a_call",
        "card-billing",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(a2a.decision, Decision::Allow);
    assert_eq!(a2a.matched_grant_id.as_deref(), Some("g-a2a"));

    // a2a_call to a different card → default-deny (grant target doesn't match)
    let a2a_other = decide_for_agent(
        &pool,
        TENANT,
        "agent-multi",
        "a2a_call",
        "card-payroll",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(a2a_other.decision, Decision::Deny);

    // skill_exec (wildcard) → allow
    let skill = decide_for_agent(
        &pool,
        TENANT,
        "agent-multi",
        "skill_exec",
        "skill-123",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(skill.decision, Decision::Allow);

    // mcp_tool → no grant of that action → default-deny (cross-action isolation)
    let mcp = decide_for_agent(
        &pool,
        TENANT,
        "agent-multi",
        "mcp_tool",
        "card-billing",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(mcp.decision, Decision::Deny);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn revoked_agent_hard_denies(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-x", false, false, "suspended").await;
    seed_grant(&pool, "g-x", "agent-x", "llm_call", "*", "allow").await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-x",
        "llm_call",
        "gpt-5.2",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(
        out.decision,
        Decision::Deny,
        "revoked credential must not act"
    );
}

// --- Constraint enforcement ---------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn rate_constraint_denies_after_limit(pool: PgPool) {
    use brutor_gateway_core::services::cache::usage_state::UsageState;
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-rate", true, true, "active").await;
    // Allow grant capped at 2 calls/hour.
    seed_grant_with_constraints(
        &pool,
        "g-rate",
        "agent-rate",
        "llm_call",
        "*",
        "allow",
        serde_json::json!({"rate": {"max": 2, "window": "hour"}}),
    )
    .await;

    // In-memory counter (no Redis) — the fallback path the test env uses.
    let us = UsageState::new(None);
    let decide = || {
        decide_for_agent(
            &pool,
            TENANT,
            "agent-rate",
            "llm_call",
            "gpt-5.2",
            None,
            None,
            Some(&us),
            &[],
        )
    };

    let o1 = decide().await.unwrap();
    let o2 = decide().await.unwrap();
    let o3 = decide().await.unwrap();
    assert_eq!(o1.decision, Decision::Allow, "1st under limit");
    assert_eq!(o2.decision, Decision::Allow, "2nd at limit");
    assert_eq!(o3.decision, Decision::Deny, "3rd over limit → denied");
    assert!(
        o3.reason.contains("rate limit"),
        "reason names the rate constraint: {}",
        o3.reason
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn max_delegation_depth_constraint_denies_when_too_deep(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-depth", true, true, "active").await;
    // Allow grant for a2a, but only up to delegation depth 2.
    seed_grant_with_constraints(
        &pool,
        "g-depth",
        "agent-depth",
        "a2a_call",
        "*",
        "allow",
        serde_json::json!({"max_delegation_depth": 2}),
    )
    .await;

    // depth 1 (within cap) → allow; depth 3 (over cap) → deny.
    let ok = decide_for_agent(
        &pool,
        TENANT,
        "agent-depth",
        "a2a_call",
        "card-x",
        None,
        Some(1),
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(ok.decision, Decision::Allow);

    let too_deep = decide_for_agent(
        &pool,
        TENANT,
        "agent-depth",
        "a2a_call",
        "card-x",
        None,
        Some(3),
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(too_deep.decision, Decision::Deny);
    assert!(
        too_deep.reason.contains("depth"),
        "reason names depth: {}",
        too_deep.reason
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn deny_tool_risk_constraint_blocks_destructive_tools(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-risk", true, true, "active").await;
    // Allow the agent to call MCP tools, but DENY anything destructive.
    seed_grant_with_constraints(
        &pool,
        "g-risk",
        "agent-risk",
        "mcp_tool",
        "*",
        "allow",
        serde_json::json!({"deny_tool_risk": ["destructive"]}),
    )
    .await;

    // A destructive tool → constraint downgrades the allow to Deny.
    let destructive = decide_for_agent(
        &pool,
        TENANT,
        "agent-risk",
        "mcp_tool",
        "delete_repo",
        None,
        None,
        None,
        &["destructive".to_string(), "open_world".to_string()],
    )
    .await
    .unwrap();
    assert_eq!(destructive.decision, Decision::Deny);
    assert!(
        destructive.reason.contains("tool risk"),
        "reason: {}",
        destructive.reason
    );

    // A read-only tool → not in the deny set → Allow.
    let read_only = decide_for_agent(
        &pool,
        TENANT,
        "agent-risk",
        "mcp_tool",
        "list_repos",
        None,
        None,
        None,
        &["read_only".to_string(), "idempotent".to_string()],
    )
    .await
    .unwrap();
    assert_eq!(read_only.decision, Decision::Allow);
}

/// A grant whose effect this build does not know is the most restrictive
/// grant, deny: it is not dropped (which, for an agent that is not
/// default-deny, would leave the resource group's allow in force).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grant_with_an_unknown_effect_denies(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    seed_agent(&pool, "agent-unknown-effect", false, true, "active").await;
    seed_grant(
        &pool,
        "g-unknown-effect",
        "agent-unknown-effect",
        "mcp_tool",
        "shell.*",
        "deny_all",
    )
    .await;
    let out = decide_for_agent(
        &pool,
        TENANT,
        "agent-unknown-effect",
        "mcp_tool",
        "shell.exec",
        Some(Decision::Allow),
        None,
        None,
        &[],
    )
    .await
    .unwrap();
    assert_eq!(out.decision, Decision::Deny);
    assert_eq!(out.matched_grant_id.as_deref(), Some("g-unknown-effect"));
}
