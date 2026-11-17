// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Autonomy resolution and enforcement.
//!
//! How much an AI System may do **without a human**. Deliberately a
//! separate axis from `agent_identities.enforcement_mode`, which decides
//! whether policy bites at all: a `dry_run` agent can still be suspended,
//! and an enforcing one can still be fully autonomous. Conflating them
//! would mean "turn this agent off" and "stop enforcing on this agent"
//! share a switch, which is the worst possible pairing.
//!
//! ## Min-wins, and why it has to be
//!
//! Resolution takes the **most restrictive** level across the resource
//! group hierarchy and the acting agent, matching how limits already
//! resolve. An agent identity may be *more* restricted than its AI System,
//! never less.
//!
//! That asymmetry is the whole point. If an actor could raise its own
//! autonomy, "suspend this AI System" would be advisory — every agent
//! inside it could opt back in, and the one control an operator reaches
//! for in an incident would be the one that does not work.
//!
//! ## Unset is not a vote
//!
//! `NULL` contributes nothing to the minimum rather than defaulting to
//! `autonomous` at each level. If an unset group voted "autonomous", any
//! group in the chain that nobody had classified would silently cancel a
//! restriction set above it, and the default state of the system would
//! quietly be "no restrictions apply".
//!
//! The default only applies when the *entire* chain is unset — that is the
//! genuine "nobody has classified this" case, and it preserves the
//! behaviour that existed before the column did.
//!
//! ## Failure is not permission
//!
//! Resolution reads the database on every governed action, which is what
//! makes suspension take effect immediately. When that read fails we fall
//! back to the last level we successfully resolved for this agent, and to
//! [`FALLBACK_FLOOR`] when there is no usable one. We never fall back to
//! `autonomous`: an operator who suspends a runaway agent must not have that
//! suspension undone by an unrelated database blip, which is exactly when
//! incidents happen and exactly when the control is being relied on.
//!
//! The floor is `restricted` rather than `suspended` on purpose. Completions
//! classify as read-only, so reads and chat keep serving through a blip while
//! nothing that can change the world proceeds unverified. Deployments that
//! prefer a total stop can set `BRUTOR_AUTONOMY_FALLBACK_FLOOR=suspended`.

use std::sync::OnceLock;
use std::time::Duration;

use moka::sync::Cache;
use serde::{Deserialize, Serialize};

/// Wire contract — mirrored in
/// `app/database/models/ai_system_contract.py`.
pub mod level {
    pub const AUTONOMOUS: &str = "autonomous";
    pub const APPROVAL_REQUIRED: &str = "approval_required";
    pub const RESTRICTED: &str = "restricted";
    pub const SUSPENDED: &str = "suspended";

    pub const ALL: [&str; 4] = [AUTONOMOUS, APPROVAL_REQUIRED, RESTRICTED, SUSPENDED];
}

/// The level a system runs at when nothing in its chain says otherwise.
///
/// This is the *unclassified* default, not the *failure* default — it applies
/// when the database answered and had nothing to say. When the database does
/// not answer at all, see [`FALLBACK_FLOOR`].
pub const DEFAULT_LEVEL: Autonomy = Autonomy::Autonomous;

/// The level applied when the governance database is unreachable and no
/// recent successful resolution is cached for this agent.
///
/// Overridable with `BRUTOR_AUTONOMY_FALLBACK_FLOOR` (any value in
/// [`level::ALL`]). An unparseable value is ignored with a warning rather
/// than panicking a running proxy — a typo in an env var must not be a
/// second outage.
pub const FALLBACK_FLOOR: Autonomy = Autonomy::Restricted;

/// How long a successfully-resolved level stays usable as an outage fallback.
///
/// Successful resolutions refresh this on every governed action, so a live
/// agent's entry is always fresh. It only ages during an outage — which
/// bounds how long a permissive cached level can outlive our ability to
/// verify it. After that, the floor applies.
const FALLBACK_MAX_STALENESS: Duration = Duration::from_secs(300);

/// Bounded so a large agent fleet cannot grow the fallback map without limit.
const FALLBACK_CACHE_CAPACITY: u64 = 50_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Autonomy {
    // Ordering is the resolution: derived `Ord` makes `min()` pick the most
    // restrictive, so the rule lives in the type rather than in a
    // comparison function somebody has to keep in sync.
    Suspended,
    Restricted,
    ApprovalRequired,
    Autonomous,
}

impl Autonomy {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            level::AUTONOMOUS => Some(Self::Autonomous),
            level::APPROVAL_REQUIRED => Some(Self::ApprovalRequired),
            level::RESTRICTED => Some(Self::Restricted),
            level::SUSPENDED => Some(Self::Suspended),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Autonomous => level::AUTONOMOUS,
            Self::ApprovalRequired => level::APPROVAL_REQUIRED,
            Self::Restricted => level::RESTRICTED,
            Self::Suspended => level::SUSPENDED,
        }
    }

    /// Whether an effecting call may proceed with no human involved.
    pub fn allows_unattended_side_effects(&self) -> bool {
        matches!(self, Self::Autonomous)
    }

    /// Whether an effecting call may proceed *at all*.
    pub fn allows_side_effects(&self) -> bool {
        matches!(self, Self::Autonomous | Self::ApprovalRequired)
    }

    pub fn is_suspended(&self) -> bool {
        matches!(self, Self::Suspended)
    }
}

/// Min-wins across a chain. Absent values contribute nothing; an entirely
/// unset chain resolves to [`DEFAULT_LEVEL`]. A level this build does not
/// know (a typo, or one a newer control plane added) is the most restrictive
/// level, [`Autonomy::Suspended`].
pub fn resolve<'a, I>(levels: I) -> Autonomy
where
    I: IntoIterator<Item = Option<&'a str>>,
{
    levels
        .into_iter()
        .flatten()
        .map(|raw| {
            Autonomy::parse(raw).unwrap_or_else(|| {
                tracing::error!(
                    level = raw,
                    "autonomy level the gateway does not know; reading it as suspended"
                );
                Autonomy::Suspended
            })
        })
        .min()
        .unwrap_or(DEFAULT_LEVEL)
}

/// What a call is asking to do. Read-only work is unaffected by everything
/// except suspension — throttling reads would make `restricted` useless as
/// a safe holding state during an investigation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallKind {
    ReadOnly,
    SideEffecting,
}

/// What the proxy should do with a call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    /// Route through the existing tool-approval flow.
    RequireApproval,
    /// Deny, with an operator-facing reason. Deliberately carries its own
    /// message: a generic 403 sends the caller to the wrong team, and
    /// "your AI System is suspended" is a five-second fix by the person who
    /// suspended it.
    Deny(&'static str),
}

pub const SUSPENDED_MESSAGE: &str =
    "This AI System is suspended. All calls are denied until an operator restores it.";
pub const RESTRICTED_MESSAGE: &str =
    "This AI System is restricted to read-only operations; side-effecting calls are denied.";
/// Deliberately distinct from [`RESTRICTED_MESSAGE`]. A degraded denial sends
/// the operator to the database, not to the autonomy config — telling them
/// their system is "restricted" when nothing is configured restricted would
/// send them hunting through settings during an outage.
pub const UNVERIFIED_MESSAGE: &str = "Authority for this AI System could not be verified (governance database unreachable); \
     side-effecting calls are denied until it recovers.";

/// How a level was arrived at. Travels with the level so the caller can record
/// it on the audit row and so degraded operation is visible rather than
/// silently indistinguishable from configured policy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Provenance {
    /// Read live from the database on this request.
    Live,
    /// The database was unreachable; this is the last level successfully
    /// resolved for this agent, still within [`FALLBACK_MAX_STALENESS`].
    LastKnownGood,
    /// The database was unreachable and no usable cached level existed.
    Floor,
}

impl Provenance {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::LastKnownGood => "last_known_good",
            Self::Floor => "floor",
        }
    }

    /// Whether this resolution reflects current configuration. `false` means
    /// the proxy is enforcing on degraded information.
    pub fn is_authoritative(&self) -> bool {
        matches!(self, Self::Live)
    }
}

/// A resolved level plus how we got it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Resolution {
    pub level: Autonomy,
    pub provenance: Provenance,
}

impl Resolution {
    pub fn live(level: Autonomy) -> Self {
        Self {
            level,
            provenance: Provenance::Live,
        }
    }
}

pub fn decide(level: Autonomy, kind: CallKind) -> Verdict {
    if level.is_suspended() {
        return Verdict::Deny(SUSPENDED_MESSAGE);
    }
    match kind {
        CallKind::ReadOnly => Verdict::Allow,
        CallKind::SideEffecting => match level {
            Autonomy::Autonomous => Verdict::Allow,
            Autonomy::ApprovalRequired => Verdict::RequireApproval,
            Autonomy::Restricted => Verdict::Deny(RESTRICTED_MESSAGE),
            Autonomy::Suspended => Verdict::Deny(SUSPENDED_MESSAGE),
        },
    }
}

/// [`decide`], but aware of how the level was obtained: a denial produced by
/// degraded resolution reports [`UNVERIFIED_MESSAGE`] instead of describing a
/// restriction the operator never configured.
///
/// Every denial under [`Provenance::Floor`] reports as unverified, including a
/// `suspended` floor: floor provenance means we do not know this system's real
/// level, so attributing the denial to configured policy would be a guess.
/// `LastKnownGood` keeps the configured message — that level *was* real.
pub fn decide_resolved(resolution: &Resolution, kind: CallKind) -> Verdict {
    let verdict = decide(resolution.level, kind);
    if resolution.provenance == Provenance::Floor {
        if let Verdict::Deny(_) = verdict {
            return Verdict::Deny(UNVERIFIED_MESSAGE);
        }
    }
    verdict
}

/// Whether this action can change the world.
///
/// `llm_call` is treated as read-only: a completion produces text, and the
/// effects come from what is done with it — which arrives here as its own
/// `mcp_tool` / `skill_exec` / `a2a_call` action and is gated then. Gating
/// completions would make `restricted` mean "cannot think", not "cannot
/// act", and nobody would use it.
///
/// Everything else is side-effecting **unless the tool declares itself
/// read-only**. Absent risk metadata fails closed: an unannotated tool is
/// exactly the one nobody has assessed.
pub fn classify(action_type: &str, tool_risk: &[String]) -> CallKind {
    if action_type == "llm_call" {
        return CallKind::ReadOnly;
    }
    if tool_risk.iter().any(|r| r == "read_only") {
        return CallKind::ReadOnly;
    }
    CallKind::SideEffecting
}

/// Last successfully-resolved level per `(tenant_id, agent_id)`, consulted
/// **only** when the database read fails.
///
/// Deliberately not a read-through cache. `resolve_for_agent` queries on every
/// governed action, and that is what makes suspension take effect immediately;
/// serving resolutions from a TTL cache would put a delay between "operator
/// hits suspend" and "agent stops", which is the one moment the control has to
/// be instant. So the happy path is unchanged and this map is written as a
/// side effect, read only in failure.
fn fallback_cache() -> &'static Cache<(String, String), Autonomy> {
    static CACHE: OnceLock<Cache<(String, String), Autonomy>> = OnceLock::new();
    CACHE.get_or_init(|| {
        Cache::builder()
            .max_capacity(FALLBACK_CACHE_CAPACITY)
            .time_to_live(FALLBACK_MAX_STALENESS)
            .build()
    })
}

/// One log line and one alert attempt per tenant per window.
///
/// Resolution runs on every governed action, so an unrated notice would turn
/// a sustained outage into a log entry and a spawned database task per call —
/// against the database that is already failing. Degradation needs to be
/// loud once, not loud continuously.
const DEGRADED_NOTIFY_COOLDOWN: Duration = Duration::from_secs(60);

fn degraded_notice_gate() -> &'static Cache<String, ()> {
    static GATE: OnceLock<Cache<String, ()>> = OnceLock::new();
    GATE.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(DEGRADED_NOTIFY_COOLDOWN)
            .build()
    })
}

/// Whether this tenant is due a degraded-mode notice. Racy by design — two
/// threads slipping through at once costs one duplicate alert, which the
/// `system_alerts` dedup key collapses anyway.
fn claim_degraded_notice(tenant_id: &str) -> bool {
    let gate = degraded_notice_gate();
    if gate.get(tenant_id).is_some() {
        return false;
    }
    gate.insert(tenant_id.to_string(), ());
    true
}

/// Surface degraded enforcement to the operator.
///
/// Enforcing on a cached or floor level is a materially different state from
/// enforcing on current configuration, and one that resolves itself silently
/// — which is exactly how it stays unnoticed until someone asks why a system
/// was denying calls for an hour.
fn notify_degraded(pool: &sqlx::PgPool, tenant_id: &str, level: Autonomy, provenance: Provenance) {
    if provenance.is_authoritative() || !claim_degraded_notice(tenant_id) {
        return;
    }
    // WARN, not ERROR: the governance database is a downstream dependency,
    // and the proxy is still enforcing — conservatively — without it.
    tracing::warn!(
        tenant_id,
        level = level.as_str(),
        provenance = provenance.as_str(),
        "autonomy enforcement degraded; governance database unreachable",
    );
    let detail = match provenance {
        Provenance::LastKnownGood => format!(
            "Autonomy is being enforced from the last level successfully resolved \
             ('{}') because the governance database is unreachable. Levels changed \
             during this window will not take effect until it recovers.",
            level.as_str()
        ),
        Provenance::Floor => format!(
            "Autonomy could not be resolved and no recent level was cached, so the \
             fallback floor ('{}') is being enforced: read-only calls proceed and \
             side-effecting calls are denied until the governance database recovers.",
            level.as_str()
        ),
        Provenance::Live => return,
    };
    crate::services::system_alerts::spawn_raise(
        pool.clone(),
        crate::services::system_alerts::SystemAlertInput::resource_health_change(
            Some(tenant_id.to_string()),
            "autonomy",
            "resolver",
            Some("Autonomy resolver".to_string()),
            "unhealthy",
            detail,
        )
        .with_title("Autonomy enforcement degraded (governance database unreachable)"),
    );
}

/// The outage floor `env` configures (`BRUTOR_AUTONOMY_FALLBACK_FLOOR`,
/// default [`FALLBACK_FLOOR`]). A value that is not an autonomy level is
/// refused: the gateway does not start with it (`config::check`).
pub fn fallback_floor_from(
    env: &crate::env_registry::Env,
) -> Result<Autonomy, crate::env_registry::EnvError> {
    let var = &crate::env_vars::BRUTOR_AUTONOMY_FALLBACK_FLOOR;
    let raw = env.required_string(var)?;
    Autonomy::parse(raw.trim()).ok_or_else(|| crate::env_vars::invalid(var, var.accepted))
}

/// The configured outage floor. Read once per process.
fn fallback_floor() -> Autonomy {
    static FLOOR: OnceLock<Autonomy> = OnceLock::new();
    // The start refused a value that is not a level; the most restrictive
    // level stands in for a floor that cannot be read at all.
    *FLOOR.get_or_init(|| {
        fallback_floor_from(&crate::env_vars::process()).unwrap_or(Autonomy::Suspended)
    })
}

/// Resolve the level applying to an agent: its own, min-wins against every
/// group it belongs to and those groups' inherited ancestors.
///
/// The ancestry walk is gated by `inherit_from_parent` and bounded, matching
/// `resolve_effective_groups` in `routes/portal_common.rs`.
///
/// On a database error this does **not** return [`DEFAULT_LEVEL`]. Falling
/// back to `autonomous` would mean a suspended agent resumes acting the moment
/// the governance database hiccups — the control an operator reaches for in an
/// incident, failing in exactly the conditions an incident produces. Instead we
/// serve the last level we successfully resolved for this agent, and
/// [`fallback_floor`] when we have none. Read-only work is unaffected either
/// way, so a blip degrades side effects rather than taking the fleet offline.
pub async fn resolve_for_agent(pool: &sqlx::PgPool, tenant_id: &str, agent_id: &str) -> Resolution {
    const SQL: &str = r#"
WITH RECURSIVE seed AS (
    SELECT rg.id, rg.parent_group_id, rg.inherit_from_parent, rg.autonomy_level, 0 AS depth
      FROM resource_group_agent_members m
      JOIN resource_groups rg ON rg.id = m.group_id
     WHERE m.tenant_id = $1 AND m.agent_id = $2
    UNION ALL
    SELECT p.id, p.parent_group_id, p.inherit_from_parent, p.autonomy_level, s.depth + 1
      FROM seed s
      JOIN resource_groups p ON p.id = s.parent_group_id
     WHERE s.inherit_from_parent = true AND s.depth < 10
)
SELECT autonomy_level FROM seed WHERE autonomy_level IS NOT NULL
UNION ALL
SELECT autonomy_level FROM agent_identities
 WHERE id = $2 AND tenant_id = $1 AND autonomy_level IS NOT NULL
"#;

    let rows = sqlx::query_as::<_, (String,)>(SQL)
        .bind(tenant_id)
        .bind(agent_id)
        .fetch_all(pool)
        .await;

    // Namespaced so a group id can never answer for an agent id, or vice
    // versa, in the shared fallback map.
    finish(pool, tenant_id, format!("a:{agent_id}"), rows)
}

/// Resolve the level applying to a **resource group** and its inherited
/// ancestors — the autonomy of an AI System independent of who is calling it.
///
/// Same walk as [`resolve_for_agent`] minus the agent's own row, and gated by
/// `inherit_from_parent` identically so the two cannot disagree about what an
/// AI System's level is.
pub async fn resolve_for_group(pool: &sqlx::PgPool, tenant_id: &str, group_id: &str) -> Resolution {
    const SQL: &str = r#"
WITH RECURSIVE seed AS (
    SELECT rg.id, rg.parent_group_id, rg.inherit_from_parent, rg.autonomy_level, 0 AS depth
      FROM resource_groups rg
     WHERE rg.id = $2 AND rg.tenant_id = $1
    UNION ALL
    SELECT p.id, p.parent_group_id, p.inherit_from_parent, p.autonomy_level, s.depth + 1
      FROM seed s
      JOIN resource_groups p ON p.id = s.parent_group_id
     WHERE s.inherit_from_parent = true AND s.depth < 10
)
SELECT autonomy_level FROM seed WHERE autonomy_level IS NOT NULL
"#;

    let rows = sqlx::query_as::<_, (String,)>(SQL)
        .bind(tenant_id)
        .bind(group_id)
        .fetch_all(pool)
        .await;

    finish(pool, tenant_id, format!("g:{group_id}"), rows)
}

/// Shared tail of both resolvers: apply min-wins on success and refresh the
/// fallback entry, or degrade deliberately on failure.
///
/// Factored out so the two resolvers cannot drift on the half that matters —
/// a future third resolver that quietly reintroduced a permissive fallback
/// would reopen the hole this whole module exists to close.
fn finish(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    scope: String,
    rows: Result<Vec<(String,)>, sqlx::Error>,
) -> Resolution {
    let key = (tenant_id.to_string(), scope);
    match rows {
        Ok(rows) => {
            let level = resolve(rows.iter().map(|(l,)| Some(l.as_str())));
            fallback_cache().insert(key, level);
            Resolution::live(level)
        }
        Err(e) => {
            let (level, provenance) = match fallback_cache().get(&key) {
                Some(level) => (level, Provenance::LastKnownGood),
                None => (fallback_floor(), Provenance::Floor),
            };
            // Rate-limited alongside the alert: the per-call detail belongs in
            // the span, not in a log line emitted once per request for the
            // duration of an outage.
            tracing::debug!(
                error = %e,
                tenant_id,
                scope = %key.1,
                level = level.as_str(),
                provenance = provenance.as_str(),
                "autonomy resolution failed; enforcing a degraded level",
            );
            notify_degraded(pool, tenant_id, level, provenance);
            Resolution { level, provenance }
        }
    }
}

/// Autonomy gate for a caller that is **not** an agent principal.
///
/// Agent callers reach autonomy through `agent_authz::decide_for_agent`, which
/// runs it as its last gate. A plain API key or human session has no agent
/// identity and so never entered that path at all — which meant suspending an
/// AI System did not stop its non-agent traffic. Suspension is a property of
/// the system, not of how the caller authenticated.
///
/// `approval_required` is deliberately **not** enforced here. It exists to
/// interpose a human where an agent would otherwise act unattended, and the
/// approval flow is keyed on an agent identity this caller does not have.
/// Denying instead would break every human and script on a system configured
/// for unattended-agent approval, which is not what that setting says.
/// `suspended` and `restricted` are unambiguous stop states and do apply.
///
/// Returns `None` when there is no group to resolve from — an unattributed
/// call has no AI System whose autonomy could apply.
pub async fn enforce_for_group(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    group_id: Option<&str>,
    action_type: &str,
    tool_risk: &[String],
) -> Option<(Verdict, Resolution)> {
    let group_id = group_id?;
    let resolution = resolve_for_group(pool, tenant_id, group_id).await;
    let verdict = match decide_resolved(&resolution, classify(action_type, tool_risk)) {
        Verdict::RequireApproval => Verdict::Allow,
        other => other,
    };
    Some((verdict, resolution))
}

/// [`enforce_for_group`] over every group a caller belongs to, strictest
/// wins: the first `Deny` is returned; otherwise the last `Allow`.
///
/// For a caller that named no group (no group-scoped key, no
/// `X-Resource-Group`), the honest scope is *all* of their memberships —
/// evaluating none of them, which is what an absent group used to mean,
/// let a member of a suspended AI System keep calling models simply by
/// omitting the header.
pub async fn enforce_for_groups(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    group_ids: &[String],
    action_type: &str,
    tool_risk: &[String],
) -> Option<(Verdict, Resolution)> {
    let mut last = None;
    for gid in group_ids {
        let outcome = enforce_for_group(pool, tenant_id, Some(gid), action_type, tool_risk).await;
        if let Some((Verdict::Deny(_), _)) = &outcome {
            return outcome;
        }
        last = outcome;
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completions_are_not_side_effecting() {
        // Otherwise `restricted` means "cannot think" rather than "cannot
        // act", and an operator investigating an incident has no safe
        // holding state.
        assert_eq!(classify("llm_call", &[]), CallKind::ReadOnly);
    }

    #[test]
    fn a_tool_with_no_risk_metadata_fails_closed() {
        // The unannotated tool is precisely the one nobody has assessed.
        assert_eq!(classify("mcp_tool", &[]), CallKind::SideEffecting);
    }

    #[test]
    fn a_declared_read_only_tool_is_read_only() {
        assert_eq!(
            classify("mcp_tool", &["read_only".to_string()]),
            CallKind::ReadOnly
        );
    }

    #[test]
    fn destructive_tools_are_side_effecting() {
        assert_eq!(
            classify("mcp_tool", &["destructive".to_string()]),
            CallKind::SideEffecting
        );
        assert_eq!(classify("skill_exec", &[]), CallKind::SideEffecting);
        assert_eq!(classify("a2a_call", &[]), CallKind::SideEffecting);
    }

    #[test]
    fn the_most_restrictive_level_wins() {
        assert_eq!(
            resolve([
                Some("autonomous"),
                Some("restricted"),
                Some("approval_required")
            ]),
            Autonomy::Restricted
        );
    }

    #[test]
    fn an_actor_cannot_raise_its_own_autonomy() {
        // The agent asks for `autonomous`; its system says `restricted`.
        // If this ever returned Autonomous, "suspend the system" would be
        // advisory and every agent inside could opt back in.
        assert_eq!(
            resolve([Some("restricted"), Some("autonomous")]),
            Autonomy::Restricted
        );
    }

    #[test]
    fn an_actor_can_lower_it() {
        assert_eq!(
            resolve([Some("autonomous"), Some("suspended")]),
            Autonomy::Suspended
        );
    }

    #[test]
    fn unset_levels_do_not_vote() {
        // If None meant "autonomous", an unclassified group anywhere in the
        // chain would silently cancel a restriction set above it.
        assert_eq!(
            resolve([Some("restricted"), None, None]),
            Autonomy::Restricted
        );
    }

    #[test]
    fn an_entirely_unset_chain_keeps_the_previous_behaviour() {
        assert_eq!(resolve([None, None]), Autonomy::Autonomous);
        assert_eq!(resolve(Vec::<Option<&str>>::new()), Autonomy::Autonomous);
    }

    #[test]
    fn an_unknown_level_is_the_most_restrictive() {
        // A typo or a level from a newer control plane must not silently
        // read as "no restriction" *or* crash the request path: it is the
        // most restrictive level there is.
        assert_eq!(
            resolve([Some("restricted"), Some("mostly_autonomous")]),
            Autonomy::Suspended
        );
        assert_eq!(resolve([Some("nonsense")]), Autonomy::Suspended);
        assert_eq!(resolve([Some("autonomous"), Some("")]), Autonomy::Suspended);
    }

    #[test]
    fn suspension_denies_reads_too() {
        // "Turn it off" has to mean off. A suspended system that can still
        // read is a system still calling providers and still costing money.
        assert_eq!(
            decide(Autonomy::Suspended, CallKind::ReadOnly),
            Verdict::Deny(SUSPENDED_MESSAGE)
        );
    }

    #[test]
    fn restricted_allows_reads_and_denies_effects() {
        assert_eq!(
            decide(Autonomy::Restricted, CallKind::ReadOnly),
            Verdict::Allow
        );
        assert_eq!(
            decide(Autonomy::Restricted, CallKind::SideEffecting),
            Verdict::Deny(RESTRICTED_MESSAGE)
        );
    }

    #[test]
    fn approval_required_routes_effects_through_a_human() {
        assert_eq!(
            decide(Autonomy::ApprovalRequired, CallKind::ReadOnly),
            Verdict::Allow
        );
        assert_eq!(
            decide(Autonomy::ApprovalRequired, CallKind::SideEffecting),
            Verdict::RequireApproval
        );
    }

    #[test]
    fn autonomous_allows_everything() {
        assert_eq!(
            decide(Autonomy::Autonomous, CallKind::ReadOnly),
            Verdict::Allow
        );
        assert_eq!(
            decide(Autonomy::Autonomous, CallKind::SideEffecting),
            Verdict::Allow
        );
    }

    #[test]
    fn denials_are_specific_enough_to_act_on() {
        // A generic 403 sends the caller to the wrong team.
        let Verdict::Deny(msg) = decide(Autonomy::Suspended, CallKind::SideEffecting) else {
            panic!("expected a denial");
        };
        assert!(msg.contains("suspended"));
        let Verdict::Deny(msg) = decide(Autonomy::Restricted, CallKind::SideEffecting) else {
            panic!("expected a denial");
        };
        assert!(msg.contains("read-only"));
    }

    #[test]
    fn the_wire_contract_matches_the_control_plane() {
        let mut all = level::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec!["approval_required", "autonomous", "restricted", "suspended"]
        );
        for name in level::ALL {
            assert_eq!(Autonomy::parse(name).unwrap().as_str(), name);
        }
    }

    #[test]
    fn ordering_is_restrictive_first() {
        // `min()` is the resolution rule; if this ordering is ever changed
        // the rule silently inverts.
        assert!(Autonomy::Suspended < Autonomy::Restricted);
        assert!(Autonomy::Restricted < Autonomy::ApprovalRequired);
        assert!(Autonomy::ApprovalRequired < Autonomy::Autonomous);
    }

    // -----------------------------------------------------------------
    // Failure handling — the kill switch must not fail open.
    //
    // The DB-backed path itself needs a live Postgres and is covered in
    // `tests/integration/autonomy_failure.rs`; these pin the decision
    // rules that path depends on, which is where a regression would
    // actually land.
    // -----------------------------------------------------------------

    #[test]
    fn the_outage_floor_is_never_autonomous() {
        // The whole point. If this ever becomes Autonomous, a suspended
        // agent resumes acting the moment the governance DB hiccups.
        assert_ne!(FALLBACK_FLOOR, Autonomy::Autonomous);
        assert!(!FALLBACK_FLOOR.allows_unattended_side_effects());
    }

    #[test]
    fn the_outage_floor_still_serves_reads() {
        // Failing closed must degrade side effects, not take the fleet
        // offline. Completions classify read-only, so chat keeps working.
        let r = Resolution {
            level: FALLBACK_FLOOR,
            provenance: Provenance::Floor,
        };
        assert_eq!(decide_resolved(&r, CallKind::ReadOnly), Verdict::Allow);
        assert_eq!(
            decide_resolved(&r, classify("llm_call", &[])),
            Verdict::Allow
        );
    }

    #[test]
    fn the_outage_floor_denies_side_effects() {
        let r = Resolution {
            level: FALLBACK_FLOOR,
            provenance: Provenance::Floor,
        };
        assert!(matches!(
            decide_resolved(&r, CallKind::SideEffecting),
            Verdict::Deny(_)
        ));
    }

    #[test]
    fn a_degraded_denial_does_not_blame_configured_policy() {
        // "restricted" would send an operator hunting through autonomy
        // settings that say nothing of the kind, during an outage.
        let r = Resolution {
            level: Autonomy::Restricted,
            provenance: Provenance::Floor,
        };
        let Verdict::Deny(msg) = decide_resolved(&r, CallKind::SideEffecting) else {
            panic!("expected a denial");
        };
        assert_eq!(msg, UNVERIFIED_MESSAGE);
        assert!(msg.contains("could not be verified"));
    }

    #[test]
    fn a_suspended_floor_also_reports_as_unverified() {
        // Floor provenance means we do not know the real level, so
        // attributing the denial to a configured suspension is a guess.
        let r = Resolution {
            level: Autonomy::Suspended,
            provenance: Provenance::Floor,
        };
        let Verdict::Deny(msg) = decide_resolved(&r, CallKind::ReadOnly) else {
            panic!("expected a denial");
        };
        assert_eq!(msg, UNVERIFIED_MESSAGE);
    }

    #[test]
    fn a_last_known_level_keeps_its_real_message() {
        // That level *was* configured, so the operator-facing reason is
        // the true one and should point at the config.
        let r = Resolution {
            level: Autonomy::Suspended,
            provenance: Provenance::LastKnownGood,
        };
        assert_eq!(
            decide_resolved(&r, CallKind::ReadOnly),
            Verdict::Deny(SUSPENDED_MESSAGE)
        );
    }

    #[test]
    fn a_live_resolution_is_unchanged_by_the_provenance_wrapper() {
        // decide_resolved must be a pure pass-through on the happy path,
        // or the fallback machinery has changed normal enforcement.
        for level in [
            Autonomy::Autonomous,
            Autonomy::ApprovalRequired,
            Autonomy::Restricted,
            Autonomy::Suspended,
        ] {
            for kind in [CallKind::ReadOnly, CallKind::SideEffecting] {
                assert_eq!(
                    decide_resolved(&Resolution::live(level), kind),
                    decide(level, kind),
                    "live {level:?} / {kind:?} diverged from the pure rule",
                );
            }
        }
    }

    #[test]
    fn provenance_reports_whether_enforcement_was_authoritative() {
        assert!(Provenance::Live.is_authoritative());
        assert!(!Provenance::LastKnownGood.is_authoritative());
        assert!(!Provenance::Floor.is_authoritative());
    }

    #[test]
    fn provenance_wire_values_are_stable() {
        // Persisted on proxy_logs and read by the Admin Console; renaming
        // one silently reclassifies historical audit rows.
        assert_eq!(Provenance::Live.as_str(), "live");
        assert_eq!(Provenance::LastKnownGood.as_str(), "last_known_good");
        assert_eq!(Provenance::Floor.as_str(), "floor");
    }

    #[test]
    fn an_invalid_floor_override_falls_back_rather_than_panicking() {
        // A typo in an env var must not be a second outage. This exercises
        // the parse arm directly; the OnceLock is process-wide so the env
        // var itself cannot be safely toggled from a test.
        assert_eq!(Autonomy::parse("not-a-level"), None);
        assert_eq!(Autonomy::parse("suspended"), Some(Autonomy::Suspended));
    }
}
