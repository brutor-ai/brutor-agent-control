// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Autonomy enforcement against a real Postgres.
//!
//! The resolution rule is a pure `min()` and is covered by unit tests. What
//! can only be proven here is the half that makes the control mean
//! anything: that the level an agent runs at is genuinely resolved from
//! **its AI System's hierarchy**, not just its own row.
//!
//! The failure that matters is specific. If the recursive walk missed the
//! parent chain — a `JOIN` that only looked at the agent's direct groups,
//! say — then suspending a parent AI System would leave every agent in its
//! children running. Nothing would error. The admin UI would show the
//! system as suspended. The agents would keep calling tools.
//!
//! That is the single control an operator reaches for in an incident, so
//! it gets a database-level test rather than a unit test over a struct.

use std::time::Duration;

use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use brutor_gateway_core::services::agent_authz::{Decision, decide_for_agent};
use brutor_gateway_core::services::autonomy::{
    self, Autonomy, CallKind, Provenance, RESTRICTED_MESSAGE, SUSPENDED_MESSAGE,
    UNVERIFIED_MESSAGE, Verdict,
};

use super::common;

// ---------------------------------------------------------------------------
// Seeding
// ---------------------------------------------------------------------------

/// Resolve, asserting the read was authoritative.
///
/// Without this assertion every case below would still pass if resolution
/// silently fell back to a cached or floor level — the tests would be
/// proving something about the fallback path instead of the SQL they exist
/// to cover.
async fn level_of(pool: &PgPool, tenant: &str, agent: &str) -> Autonomy {
    let r = autonomy::resolve_for_agent(pool, tenant, agent).await;
    assert!(
        r.provenance.is_authoritative(),
        "expected a live resolution for {agent}, got {:?}",
        r.provenance,
    );
    r.level
}

async fn seed_group(
    pool: &PgPool,
    tenant: &str,
    id: &str,
    parent: Option<&str>,
    inherits: bool,
    autonomy_level: Option<&str>,
) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind, parent_group_id, autonomy_level) \
         VALUES ($1, $2, 0, $3, $1, true, false, 'AI_SYSTEM', 'agent', $4, $5) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(inherits)
    .bind(parent)
    .bind(autonomy_level)
    .execute(pool)
    .await
    .expect("seed_group");
}

async fn seed_agent(pool: &PgPool, tenant: &str, id: &str, autonomy_level: Option<&str>) {
    sqlx::query(
        "INSERT INTO agent_identities \
         (id, tenant_id, name, source, enabled, status, default_deny, \
          enforcement_mode, autonomy_level) \
         VALUES ($1, $2, $1, 'manual', true, 'active', false, 'enforce', $3) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .bind(autonomy_level)
    .execute(pool)
    .await
    .expect("seed_agent");
}

async fn join_group(pool: &PgPool, tenant: &str, agent: &str, group: &str) {
    sqlx::query(
        // `id` is a serial here, unlike most tables in this schema.
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id, role) \
         VALUES ($1, $2, $3, 'member') ON CONFLICT DO NOTHING",
    )
    .bind(tenant)
    .bind(agent)
    .bind(group)
    .execute(pool)
    .await
    .expect("join_group");
}

async fn allow_grant(pool: &PgPool, tenant: &str, agent: &str, action: &str) {
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ($1, $2, $3, $4, '*', 'allow', true) ON CONFLICT DO NOTHING",
    )
    .bind(format!("g-{agent}-{action}"))
    .bind(tenant)
    .bind(agent)
    .bind(action)
    .execute(pool)
    .await
    .expect("allow_grant");
}

async fn authorize(
    pool: &PgPool,
    tenant: &str,
    agent: &str,
    action: &str,
    tool_risk: &[String],
) -> (Decision, String) {
    let outcome = decide_for_agent(
        pool,
        tenant,
        agent,
        action,
        "some-tool",
        None,
        None,
        None,
        tool_risk,
    )
    .await
    .expect("query");
    (outcome.decision, outcome.reason)
}

// ---------------------------------------------------------------------------
// Resolution across the hierarchy
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_parents_restriction_reaches_an_agent_in_the_child(pool: PgPool) {
    // The test this file exists for. Suspending a parent AI System must
    // stop the agents in its inheriting children; a walk that only looked
    // at direct memberships would leave them running while the UI showed
    // the system as suspended.
    let t = "t-au-parent";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "parent", None, true, Some("suspended")).await;
    seed_group(&pool, t, "child", Some("parent"), true, None).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "child").await;

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Suspended);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_non_inheriting_child_is_not_reached(pool: PgPool) {
    // `inherit_from_parent = false` is a real boundary elsewhere in the
    // platform; autonomy must not quietly ignore it, or the contract's
    // description of the closure and the runtime's behaviour disagree.
    let t = "t-au-noinherit";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "parent", None, true, Some("suspended")).await;
    seed_group(&pool, t, "child", Some("parent"), false, None).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "child").await;

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Autonomous);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_can_tighten_but_not_loosen(pool: PgPool) {
    let t = "t-au-tighten";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("approval_required")).await;

    seed_agent(&pool, t, "stricter", Some("restricted")).await;
    join_group(&pool, t, "stricter", "sys").await;
    assert_eq!(level_of(&pool, t, "stricter").await, Autonomy::Restricted);

    seed_agent(&pool, t, "greedier", Some("autonomous")).await;
    join_group(&pool, t, "greedier", "sys").await;
    assert_eq!(
        level_of(&pool, t, "greedier").await,
        Autonomy::ApprovalRequired,
        "an agent that asks for more freedom than its system allows must not get it — \
         otherwise 'suspend this AI System' is advisory"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn membership_of_two_systems_takes_the_stricter(pool: PgPool) {
    let t = "t-au-two";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "loose", None, true, Some("autonomous")).await;
    seed_group(&pool, t, "tight", None, true, Some("restricted")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "loose").await;
    join_group(&pool, t, "ag", "tight").await;

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Restricted);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unclassified_fleet_behaves_as_it_did_before_the_column(pool: PgPool) {
    let t = "t-au-unset";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, None).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;

    assert_eq!(
        level_of(&pool, t, "ag").await,
        Autonomy::Autonomous,
        "adding this column must not silently interpose a human in every \
         side-effecting call on every existing system"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_agent_in_no_group_falls_back_to_its_own_level(pool: PgPool) {
    let t = "t-au-orphan";
    common::seed_tenant(&pool, t).await;
    seed_agent(&pool, t, "ag", Some("restricted")).await;

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Restricted);
}

// ---------------------------------------------------------------------------
// Enforcement through the authz path
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspended_system_denies_an_otherwise_allowed_call(pool: PgPool) {
    let t = "t-au-enf-susp";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("suspended")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "mcp_tool").await;

    let (decision, reason) = authorize(&pool, t, "ag", "mcp_tool", &[]).await;
    assert_eq!(decision, Decision::Deny);
    assert_eq!(
        reason, SUSPENDED_MESSAGE,
        "a generic 403 sends the caller to the wrong team"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn suspension_wins_over_dry_run(pool: PgPool) {
    // `enforcement_mode = dry_run` is about whether *policy* bites.
    // Suspension is not a policy experiment: an operator who suspends a
    // runaway agent and watches it keep acting has a control that does not
    // work.
    let t = "t-au-dryrun";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("suspended")).await;
    seed_agent(&pool, t, "ag", None).await;
    sqlx::query("UPDATE agent_identities SET enforcement_mode = 'dry_run' WHERE id = 'ag'")
        .execute(&pool)
        .await
        .unwrap();
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "mcp_tool").await;

    let outcome = decide_for_agent(
        &pool,
        t,
        "ag",
        "mcp_tool",
        "some-tool",
        None,
        None,
        None,
        &[],
    )
    .await
    .unwrap();

    assert_eq!(outcome.decision, Decision::Deny);
    assert!(
        !outcome.dry_run,
        "a suspension must not be shadow-logged and waved through"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_restricted_system_still_permits_reads(pool: PgPool) {
    let t = "t-au-enf-read";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("restricted")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "mcp_tool").await;

    let (read, _) = authorize(&pool, t, "ag", "mcp_tool", &["read_only".into()]).await;
    assert_eq!(
        read,
        Decision::Allow,
        "`restricted` has to be a usable holding state during an investigation — \
         if it blocked reads nobody would reach for it"
    );

    let (write, reason) = authorize(&pool, t, "ag", "mcp_tool", &["destructive".into()]).await;
    assert_eq!(write, Decision::Deny);
    assert_eq!(reason, RESTRICTED_MESSAGE);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_downgrades_an_allow(pool: PgPool) {
    let t = "t-au-enf-appr";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("approval_required")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "mcp_tool").await;

    let (decision, _) = authorize(&pool, t, "ag", "mcp_tool", &[]).await;
    assert_eq!(decision, Decision::ApprovalRequired);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn completions_are_unaffected_by_restriction(pool: PgPool) {
    let t = "t-au-enf-llm";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("restricted")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "llm_call").await;

    let (decision, _) = authorize(&pool, t, "ag", "llm_call", &[]).await;
    assert_eq!(
        decision,
        Decision::Allow,
        "the effects come from what is done with a completion, and those arrive \
         as their own gated actions"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn autonomy_never_widens_an_existing_denial(pool: PgPool) {
    // Every other gate in this function can only tighten; autonomy must be
    // no different, or an `autonomous` level would override a grant denial.
    let t = "t-au-enf-deny";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("autonomous")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('deny-1', $1, 'ag', 'mcp_tool', '*', 'deny', true)",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    let (decision, _) = authorize(&pool, t, "ag", "mcp_tool", &[]).await;
    assert_eq!(decision, Decision::Deny);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_pure_decision_and_the_db_path_agree(pool: PgPool) {
    // A cheap consistency check between the unit-tested rule and what the
    // request path actually does with it.
    let t = "t-au-agree";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("restricted")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;
    allow_grant(&pool, t, "ag", "mcp_tool").await;

    let level = level_of(&pool, t, "ag").await;
    assert_eq!(
        autonomy::decide(level, CallKind::SideEffecting),
        Verdict::Deny(RESTRICTED_MESSAGE)
    );
    let (decision, _) = authorize(&pool, t, "ag", "mcp_tool", &[]).await;
    assert_eq!(decision, Decision::Deny);
}

// ---------------------------------------------------------------------------
// Failure handling
//
// The kill switch reads the database on every governed action, which is what
// makes suspension immediate. That also means a database failure is a
// decision point, and the wrong answer there is the expensive one: if
// resolution fell back to `autonomous`, suspending a runaway agent would be
// undone by an unrelated DB blip — during exactly the incident that made
// someone reach for the control.
//
// These need a pool that genuinely errors, so they use a lazily-connected
// pool pointing at a closed port rather than a mock.
// ---------------------------------------------------------------------------

/// A pool that resolves to a connection error on first use. Port 1 is
/// reserved and refuses immediately, so this fails fast rather than waiting
/// out the acquire timeout.
fn unreachable_pool() -> PgPool {
    PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_millis(500))
        .connect_lazy("postgres://nobody:nobody@127.0.0.1:1/nonexistent")
        .expect("lazy pool construction should not itself fail")
}

#[tokio::test]
async fn an_unreachable_database_never_resolves_to_autonomous() {
    // The regression this section exists for. A cold cache plus a dead DB
    // must not hand back the most permissive level in the enum.
    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, "t-au-cold", "never-seen").await;

    assert_eq!(r.provenance, Provenance::Floor);
    assert_ne!(
        r.level,
        Autonomy::Autonomous,
        "a database outage must not grant autonomy",
    );
    assert!(!r.level.allows_unattended_side_effects());
}

#[tokio::test]
async fn an_unreachable_database_still_serves_reads() {
    // Failing closed has to degrade side effects, not take the fleet
    // offline. Completions classify read-only, so chat keeps working.
    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, "t-au-cold-read", "never-seen").await;

    assert_eq!(
        autonomy::decide_resolved(&r, CallKind::ReadOnly),
        Verdict::Allow
    );
    assert_eq!(
        autonomy::decide_resolved(&r, autonomy::classify("llm_call", &[])),
        Verdict::Allow,
    );
}

#[tokio::test]
async fn an_unreachable_database_denies_side_effects_as_unverified() {
    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, "t-au-cold-write", "never-seen").await;

    let Verdict::Deny(msg) = autonomy::decide_resolved(&r, CallKind::SideEffecting) else {
        panic!("a side-effecting call under an unreachable DB must be denied");
    };
    assert_eq!(
        msg, UNVERIFIED_MESSAGE,
        "the denial must point at the outage, not at a restriction nobody configured",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_suspension_survives_the_database_going_away(pool: PgPool) {
    // The scenario in full: an operator suspends a runaway agent, then the
    // governance database becomes unreachable. The suspension must hold.
    let t = "t-au-outage-suspend";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("suspended")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;

    // Live read — establishes the level and warms the fallback entry.
    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Suspended);

    // Database goes away.
    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, t, "ag").await;

    assert_eq!(r.provenance, Provenance::LastKnownGood);
    assert_eq!(r.level, Autonomy::Suspended);
    assert_eq!(
        autonomy::decide_resolved(&r, CallKind::ReadOnly),
        Verdict::Deny(SUSPENDED_MESSAGE),
        "a suspended agent must stay suspended through an outage, and the \
         reason must still name the suspension the operator applied",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_cached_level_is_not_confused_between_agents(pool: PgPool) {
    // The fallback map is process-wide. If it were keyed loosely, one
    // agent's cached level could answer for another — including answering
    // "autonomous" for a suspended one.
    let t = "t-au-outage-keys";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "loose", None, true, Some("autonomous")).await;
    seed_agent(&pool, t, "permissive", None).await;
    join_group(&pool, t, "permissive", "loose").await;

    assert_eq!(level_of(&pool, t, "permissive").await, Autonomy::Autonomous);

    let dead = unreachable_pool();
    let other = autonomy::resolve_for_agent(&dead, t, "a-different-agent").await;

    assert_eq!(
        other.provenance,
        Provenance::Floor,
        "an unrelated agent must not inherit a cached level",
    );
    assert_ne!(other.level, Autonomy::Autonomous);
}

// ---------------------------------------------------------------------------
// Non-agent callers
//
// The autonomy gate used to live only inside `decide_for_agent`, which runs
// only when the caller carries an agent identity. A plain API key pointed at
// a suspended AI System therefore kept working — the one control an operator
// reaches for in an incident, exempting exactly the callers a script uses.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn suspension_reaches_a_caller_with_no_agent_identity(pool: PgPool) {
    let t = "t-au-nonagent-susp";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("suspended")).await;

    let (verdict, resolution) = autonomy::enforce_for_group(&pool, t, Some("sys"), "mcp_tool", &[])
        .await
        .expect("a group was supplied, so a decision must be returned");

    assert_eq!(resolution.level, Autonomy::Suspended);
    assert!(
        matches!(verdict, Verdict::Deny(_)),
        "a suspended AI System must stop a non-agent caller",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn suspension_reaches_a_non_agent_caller_through_inheritance(pool: PgPool) {
    // Same ancestry rules as the agent path — suspending a parent must stop
    // non-agent traffic against a child.
    let t = "t-au-nonagent-inherit";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "parent", None, true, Some("suspended")).await;
    seed_group(&pool, t, "child", Some("parent"), true, None).await;

    let (verdict, resolution) =
        autonomy::enforce_for_group(&pool, t, Some("child"), "llm_call", &[])
            .await
            .expect("decision");

    assert_eq!(resolution.level, Autonomy::Suspended);
    assert!(matches!(verdict, Verdict::Deny(_)));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_restricted_system_stops_non_agent_writes_but_not_reads(pool: PgPool) {
    let t = "t-au-nonagent-restricted";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("restricted")).await;

    let (write, _) = autonomy::enforce_for_group(&pool, t, Some("sys"), "mcp_tool", &[])
        .await
        .expect("decision");
    assert!(matches!(write, Verdict::Deny(_)), "writes must stop");

    // A completion is read-only, so `restricted` must leave chat working.
    let (read, _) = autonomy::enforce_for_group(&pool, t, Some("sys"), "llm_call", &[])
        .await
        .expect("decision");
    assert_eq!(
        read,
        Verdict::Allow,
        "restricted must remain a safe holding state"
    );

    // As must a tool that declares itself read-only.
    let (annotated, _) = autonomy::enforce_for_group(
        &pool,
        t,
        Some("sys"),
        "mcp_tool",
        &["read_only".to_string()],
    )
    .await
    .expect("decision");
    assert_eq!(annotated, Verdict::Allow);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_required_does_not_block_a_non_agent_caller(pool: PgPool) {
    // `approval_required` exists to interpose a human where an agent would
    // otherwise act unattended, and the approval flow is keyed on an agent
    // identity this caller does not have. Denying instead would break every
    // human and script on a system configured for unattended-agent approval.
    let t = "t-au-nonagent-approval";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("approval_required")).await;

    let (verdict, resolution) = autonomy::enforce_for_group(&pool, t, Some("sys"), "mcp_tool", &[])
        .await
        .expect("decision");

    assert_eq!(resolution.level, Autonomy::ApprovalRequired);
    assert_eq!(
        verdict,
        Verdict::Allow,
        "approval_required is an agent-unattended control, not a block on humans",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unclassified_system_does_not_start_blocking_non_agent_callers(pool: PgPool) {
    // Closing this hole must not interpose a new denial on the existing fleet.
    let t = "t-au-nonagent-unset";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, None).await;

    let (verdict, resolution) = autonomy::enforce_for_group(&pool, t, Some("sys"), "mcp_tool", &[])
        .await
        .expect("decision");

    assert_eq!(resolution.level, Autonomy::Autonomous);
    assert_eq!(verdict, Verdict::Allow);
}

#[tokio::test]
async fn an_unattributed_call_has_no_system_to_enforce() {
    // No group means no AI System, so there is no autonomy that could apply.
    // Returning a denial here would block every unattributed call in the
    // platform; returning None leaves it to the layers that do apply.
    let dead = unreachable_pool();
    assert!(
        autonomy::enforce_for_group(&dead, "t-au-none", None, "mcp_tool", &[])
            .await
            .is_none(),
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_group_and_agent_resolvers_agree_on_a_systems_level(pool: PgPool) {
    // Two resolvers, one question. If they diverged, "what is this system's
    // autonomy" would have two answers depending on who asked.
    let t = "t-au-resolver-agree";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("restricted")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;

    let via_agent = level_of(&pool, t, "ag").await;
    let via_group = autonomy::resolve_for_group(&pool, t, "sys").await;

    assert_eq!(via_group.provenance, Provenance::Live);
    assert_eq!(via_agent, via_group.level);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_cache_entry_cannot_answer_for_an_agent(pool: PgPool) {
    // The fallback map is shared between both resolvers. If its keys were not
    // namespaced, a permissive group's cached level could answer for an agent
    // of the same id — or the reverse.
    let t = "t-au-ns";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "collide", None, true, Some("autonomous")).await;

    // Warm the *group* entry under the id "collide".
    assert_eq!(
        autonomy::resolve_for_group(&pool, t, "collide").await.level,
        Autonomy::Autonomous,
    );

    // An *agent* with the same id must not pick that up during an outage.
    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, t, "collide").await;
    assert_eq!(
        r.provenance,
        Provenance::Floor,
        "agent and group cache keys must not collide",
    );
    assert_ne!(r.level, Autonomy::Autonomous);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_live_read_refreshes_what_the_outage_path_will_serve(pool: PgPool) {
    // An operator tightens the level, then the DB fails. The outage must
    // serve the tightened level, not the one cached before the change.
    let t = "t-au-outage-refresh";
    common::seed_tenant(&pool, t).await;
    seed_group(&pool, t, "sys", None, true, Some("autonomous")).await;
    seed_agent(&pool, t, "ag", None).await;
    join_group(&pool, t, "ag", "sys").await;

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Autonomous);

    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind("sys")
        .execute(&pool)
        .await
        .expect("tighten the level");

    assert_eq!(level_of(&pool, t, "ag").await, Autonomy::Suspended);

    let dead = unreachable_pool();
    let r = autonomy::resolve_for_agent(&dead, t, "ag").await;
    assert_eq!(
        r.level,
        Autonomy::Suspended,
        "the outage path must serve the most recent successful resolution",
    );
}
