// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// Agent runtime authorization — "actions, not access".
//
// When a request's actor is an agent principal (Agent Identity), every
// governed action (LLM call, MCP tool call, A2A call, skill execution) is
// decided here against that agent's grants, AND combined strictest-wins with
// the existing resource-group ACL decision. This is the per-agent enforcement
// plane: a decision per action, not a static access grant.
//
// This module is the PURE decision core (no DB, no async) so it can be
// exhaustively unit-tested. The DB load of an agent's grants and the wiring
// into the request path live alongside the route handlers; they call
// `decide()` with grants already materialized.

/// The three possible authorization effects, ordered by strictness.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    Allow,
    ApprovalRequired,
    Deny,
}

impl Decision {
    /// Strictness rank — higher is stricter. Used for strictest-wins merges.
    fn rank(self) -> u8 {
        match self {
            Decision::Allow => 1,
            Decision::ApprovalRequired => 2,
            Decision::Deny => 3,
        }
    }

    /// The stricter of two decisions.
    pub fn strictest(self, other: Decision) -> Decision {
        if self.rank() >= other.rank() {
            self
        } else {
            other
        }
    }

    pub fn as_str(self) -> &'static str {
        use crate::constants::agent_decision;
        match self {
            Decision::Allow => agent_decision::ALLOW,
            Decision::ApprovalRequired => agent_decision::APPROVAL_REQUIRED,
            Decision::Deny => agent_decision::DENY,
        }
    }
}

/// Per-grant `constraints` (the `agent_grants.constraints` JSONB). All optional;
/// an absent field is "no constraint". A matched grant whose constraints are
/// violated is downgraded to `Deny`.
#[derive(Debug, Clone, Default)]
pub struct GrantConstraints {
    /// Rate limit: at most `max` matched actions per `window`.
    pub rate: Option<RateConstraint>,
    /// Allowed wall-clock window (UTC). The grant is inert outside it.
    pub time_window: Option<TimeWindowConstraint>,
    /// Max A2A delegation depth this grant authorizes (enforced where a request
    /// depth is supplied; the signed group-level cap remains authoritative).
    pub max_delegation_depth: Option<u32>,
    /// Deny the action if the tool's risk attributes intersect this set
    /// (e.g. `["destructive"]` blocks any MCP tool flagged destructive). Risk
    /// attributes derive from the MCP tool annotation hints; tokens:
    /// `read_only` | `destructive` | `idempotent` | `open_world`.
    pub deny_tool_risk: Option<Vec<String>>,
}

/// `{ "max": <n>, "window": "hour"|"day"|"month" }`.
#[derive(Debug, Clone)]
pub struct RateConstraint {
    pub max: i64,
    /// "hour" (default) | "day" | "month".
    pub window: String,
}

/// `{ "days": [0..6], "start": "HH:MM", "end": "HH:MM" }` — all UTC, `days`
/// uses 0=Sunday..6=Saturday. Any field may be omitted.
#[derive(Debug, Clone, Default)]
pub struct TimeWindowConstraint {
    pub days: Option<Vec<u32>>,
    pub start: Option<String>,
    pub end: Option<String>,
}

impl GrantConstraints {
    /// Tolerant parse from the stored JSON; unknown shapes degrade to "no
    /// constraint" rather than erroring (a malformed constraint must never
    /// silently widen access, but it also must not 500 the request — the
    /// matched grant simply keeps its base effect).
    pub fn from_json(v: Option<&serde_json::Value>) -> Self {
        let Some(obj) = v.and_then(|v| v.as_object()) else {
            return Self::default();
        };
        let rate = obj.get("rate").and_then(|r| r.as_object()).and_then(|r| {
            let max = r.get("max").and_then(|m| m.as_i64())?;
            let window = r
                .get("window")
                .and_then(|w| w.as_str())
                .unwrap_or("hour")
                .to_string();
            Some(RateConstraint { max, window })
        });
        let time_window = obj.get("time_window").and_then(|t| t.as_object()).map(|t| {
            let days = t.get("days").and_then(|d| d.as_array()).map(|arr| {
                arr.iter()
                    .filter_map(|x| x.as_u64().map(|n| n as u32))
                    .collect::<Vec<_>>()
            });
            TimeWindowConstraint {
                days,
                start: t.get("start").and_then(|s| s.as_str()).map(str::to_owned),
                end: t.get("end").and_then(|s| s.as_str()).map(str::to_owned),
            }
        });
        let max_delegation_depth = obj
            .get("max_delegation_depth")
            .and_then(|d| d.as_u64())
            .map(|d| d as u32);
        let deny_tool_risk = obj
            .get("deny_tool_risk")
            .and_then(|d| d.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|x| x.as_str().map(str::to_owned))
                    .collect::<Vec<_>>()
            });
        Self {
            rate,
            time_window,
            max_delegation_depth,
            deny_tool_risk,
        }
    }
}

/// Compute a tool's risk-attribute set from its MCP annotation hints, applying
/// the MCP spec defaults (a tool with no annotations is treated as
/// `destructive` + `open_world` — conservative for governance). Pure.
pub fn tool_risk_attributes(annotations: Option<&serde_json::Value>) -> Vec<String> {
    let ann = annotations.and_then(|a| a.as_object());
    let hint = |k: &str, default: bool| -> bool {
        ann.and_then(|o| o.get(k))
            .and_then(|v| v.as_bool())
            .unwrap_or(default)
    };
    let read_only = hint("readOnlyHint", false);
    // Spec: destructiveHint default true, but meaningless (false) for read-only.
    let destructive = if read_only {
        false
    } else {
        hint("destructiveHint", true)
    };
    // Spec: idempotentHint default false; a read-only tool is idempotent.
    let idempotent = if read_only {
        true
    } else {
        hint("idempotentHint", false)
    };
    let open_world = hint("openWorldHint", true);
    let mut attrs = Vec::new();
    if read_only {
        attrs.push("read_only".to_string());
    }
    if destructive {
        attrs.push("destructive".to_string());
    }
    if idempotent {
        attrs.push("idempotent".to_string());
    }
    if open_world {
        attrs.push("open_world".to_string());
    }
    attrs
}

/// Parse "HH:MM" to minutes-since-midnight.
fn parse_hhmm(s: &str) -> Option<u32> {
    let (h, m) = s.split_once(':')?;
    let h: u32 = h.parse().ok()?;
    let m: u32 = m.parse().ok()?;
    if h > 23 || m > 59 {
        return None;
    }
    Some(h * 60 + m)
}

/// Whether `now` (UTC) falls inside the time window. A window with no fields is
/// always open. Supports overnight ranges (start > end).
pub fn time_window_allows(tw: &TimeWindowConstraint, now: chrono::DateTime<chrono::Utc>) -> bool {
    use chrono::{Datelike, Timelike};
    if let Some(days) = &tw.days {
        // chrono weekday: Mon=0..Sun=6 via num_days_from_monday; we want
        // 0=Sun..6=Sat, so map.
        let dow_sun0 = now.weekday().num_days_from_sunday();
        if !days.contains(&dow_sun0) {
            return false;
        }
    }
    let start = tw.start.as_deref().and_then(parse_hhmm);
    let end = tw.end.as_deref().and_then(parse_hhmm);
    if start.is_none() && end.is_none() {
        return true;
    }
    let now_min = now.hour() * 60 + now.minute();
    match (start, end) {
        (Some(s), Some(e)) if s <= e => now_min >= s && now_min < e,
        (Some(s), Some(e)) => now_min >= s || now_min < e, // overnight window
        (Some(s), None) => now_min >= s,
        (None, Some(e)) => now_min < e,
        (None, None) => true,
    }
}

/// Map a window string to a `QuotaWindow` (defaults to hour).
pub fn rate_quota_window(window: &str) -> crate::services::cache::usage_state::QuotaWindow {
    use crate::services::cache::usage_state::QuotaWindow;
    match window {
        "day" => QuotaWindow::Day,
        "month" => QuotaWindow::Month,
        _ => QuotaWindow::Hour,
    }
}

/// One agent grant, already materialized from `agent_grants`.
#[derive(Debug, Clone)]
pub struct Grant {
    pub id: String,
    /// "llm_call" | "mcp_tool" | "a2a_call" | "skill_exec".
    pub action_type: String,
    /// Glob over the action target (model / tool / card / skill). "*" = any.
    pub target: String,
    pub effect: Decision,
    pub enabled: bool,
    /// Unix seconds; `None` = never expires.
    pub expires_at: Option<i64>,
    /// Parsed `constraints` JSON (rate / time-window / depth).
    pub constraints: GrantConstraints,
}

impl Grant {
    fn is_live(&self, now_unix: i64) -> bool {
        if !self.enabled || self.expires_at.map_or(false, |e| e <= now_unix) {
            return false;
        }
        // A time-windowed grant is inert outside its window.
        if let Some(tw) = &self.constraints.time_window {
            let now = chrono::DateTime::<chrono::Utc>::from_timestamp(now_unix, 0)
                .unwrap_or_else(chrono::Utc::now);
            if !time_window_allows(tw, now) {
                return false;
            }
        }
        true
    }
}

/// The outcome of an authorization decision, with the matched grant (if any)
/// for evidence stamping on proxy_logs.
#[derive(Debug, Clone)]
pub struct Outcome {
    pub decision: Decision,
    pub matched_grant_id: Option<String>,
    pub reason: String,
    /// The matched grant's `max_delegation_depth` constraint, surfaced so a
    /// caller that knows the request's delegation depth can enforce it.
    pub max_delegation_depth: Option<u32>,
    /// When true (agent `enforcement_mode = dry_run`), a non-Allow decision is
    /// recorded as a would-block but NOT enforced — the caller logs a shadow
    /// row and proceeds. Always false for the inactive/revoked hard-deny (a
    /// revoked credential is a security boundary, not a policy in dry-run).
    pub dry_run: bool,
    /// The autonomy level in force for this call, once the autonomy gate has
    /// run. `None` when the decision was already `Deny` before that gate, so
    /// the level was never resolved — recorded as absent rather than guessed.
    pub autonomy_level: Option<&'static str>,
    /// How [`Self::autonomy_level`] was obtained (`live` / `last_known_good` /
    /// `floor`). Persisted alongside the level so a denial taken on degraded
    /// information is distinguishable after the fact from one taken on
    /// current configuration.
    pub autonomy_provenance: Option<&'static str>,
}

/// Decide whether an agent may perform `action_type` against `target`.
///
/// - `grants`: the agent's grants (any action type; filtered here).
/// - `rg_decision`: the existing resource-group ACL decision for the same
///   action, if the RG layer expressed one. Combined strictest-wins.
/// - `default_deny`: when the agent has no matching grant, deny (true) or
///   defer to the RG decision / allow (false).
///
/// Strictest-wins everywhere: an explicit `Deny` from either plane wins; an
/// `ApprovalRequired` beats `Allow`.
pub fn decide(
    grants: &[Grant],
    action_type: &str,
    target: &str,
    now_unix: i64,
    default_deny: bool,
    rg_decision: Option<Decision>,
) -> Outcome {
    // Strictest matching grant for this action+target.
    let mut agent: Option<&Grant> = None;
    for g in grants {
        if !g.is_live(now_unix) {
            continue;
        }
        if g.action_type != action_type {
            continue;
        }
        if !glob_match(&g.target, target) {
            continue;
        }
        let better = match agent {
            None => true,
            Some(cur) => g.effect.rank() > cur.effect.rank(),
        };
        if better {
            agent = Some(g);
        }
    }
    let matched_depth_cap = agent.and_then(|g| g.constraints.max_delegation_depth);

    // Agent-plane decision (before merging with the RG plane).
    let (agent_decision, matched_id, base_reason): (Option<Decision>, Option<String>, String) =
        match agent {
            Some(g) => (
                Some(g.effect),
                Some(g.id.clone()),
                format!("agent grant: {}", g.effect.as_str()),
            ),
            None => {
                if default_deny {
                    (
                        Some(Decision::Deny),
                        None,
                        "agent default-deny: no matching grant".to_string(),
                    )
                } else {
                    (
                        None,
                        None,
                        "no agent grant; deferring to resource group".to_string(),
                    )
                }
            }
        };

    // Merge strictest-wins with the resource-group decision.
    let final_decision = match (agent_decision, rg_decision) {
        (Some(a), Some(r)) => a.strictest(r),
        (Some(a), None) => a,
        (None, Some(r)) => r,
        (None, None) => Decision::Allow,
    };

    // The matched grant id is only meaningful when the agent plane is what
    // drove (or tied) the final decision.
    let matched = match agent_decision {
        Some(a) if a.rank() >= rg_decision.map_or(0, |r| r.rank()) => matched_id,
        _ => None,
    };

    let reason = match (agent_decision, rg_decision) {
        (Some(a), Some(r)) if r.rank() > a.rank() => {
            format!(
                "resource group stricter ({}) than {}",
                r.as_str(),
                base_reason
            )
        }
        _ => base_reason,
    };

    Outcome {
        decision: final_decision,
        matched_grant_id: matched,
        reason,
        max_delegation_depth: matched_depth_cap,
        // Pure decision core is mode-agnostic; decide_for_agent stamps the
        // agent's enforcement_mode after loading it.
        dry_run: false,
        // Likewise: the pure core does not touch the database, so it cannot
        // resolve autonomy. decide_for_agent stamps these after its gate runs.
        autonomy_level: None,
        autonomy_provenance: None,
    }
}

/// Minimal, dependency-free glob: `*` matches any run of characters
/// (including empty). Case-sensitive. `"*"` matches everything.
pub fn glob_match(pattern: &str, text: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') {
        return pattern == text;
    }
    let parts: Vec<&str> = pattern.split('*').collect();
    let mut pos = 0usize;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        if i == 0 {
            // Anchored prefix.
            if !text[pos..].starts_with(part) {
                return false;
            }
            pos += part.len();
        } else if i == parts.len() - 1 {
            // Anchored suffix.
            return text[pos..].ends_with(part);
        } else {
            // Floating middle segment.
            match text[pos..].find(part) {
                Some(idx) => pos += idx + part.len(),
                None => return false,
            }
        }
    }
    true
}

// ---------------------------------------------------------------------------
// DB layer — load an agent's grants and decide. Async, needs a live Postgres
// (covered by the db-tests integration suite). Kept thin: the judgment lives
// in the pure `decide` above.
// ---------------------------------------------------------------------------

/// Whether the call needs a grant of the agent's to be allowed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantRequirement {
    /// The rule: a default-deny agent with no matching grant is denied.
    Required,
    /// The platform's own call made for the agent (an embedding for the
    /// semantic cache or the Knowledge Base, verified by
    /// `internal_call::internal_embedding`): the
    /// absence of a grant does not deny it. Nothing else changes — an
    /// agent that is disabled, revoked or not active is denied, an explicit
    /// `deny` or `approval_required` grant that matches still applies with
    /// its constraints, and the autonomy gate still runs.
    NotRequired,
}

/// Load the agent's `default_deny` + live grants for `action_type` and decide.
///
/// Always decides: an agent with no identity row in the tenant is denied
/// (never `Ok(None)`, "no gate"), and an inactive/revoked
/// agent hard-denies (a revoked credential must not act). There is no "no
/// gate" answer, and the return type has none.
#[allow(clippy::too_many_arguments)]
pub async fn decide_for_agent(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    agent_id: &str,
    action_type: &str,
    target: &str,
    rg_decision: Option<Decision>,
    request_depth: Option<u32>,
    usage_state: Option<&crate::services::cache::usage_state::UsageState>,
    tool_risk: &[String],
) -> Result<Outcome, sqlx::Error> {
    decide_for_agent_with(
        pool,
        tenant_id,
        agent_id,
        action_type,
        target,
        rg_decision,
        request_depth,
        usage_state,
        tool_risk,
        GrantRequirement::Required,
    )
    .await
}

/// [`decide_for_agent`], stating whether the agent's grant is required.
pub async fn decide_for_agent_with(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    agent_id: &str,
    action_type: &str,
    target: &str,
    rg_decision: Option<Decision>,
    // The request's A2A delegation depth, if known — enables the per-grant
    // `max_delegation_depth` constraint. `None` on surfaces without a depth.
    request_depth: Option<u32>,
    // Redis-backed counter store for the per-grant `rate` constraint. `None`
    // skips rate enforcement.
    usage_state: Option<&crate::services::cache::usage_state::UsageState>,
    // The action target's risk attributes (e.g. an MCP tool's
    // read_only/destructive/... set), for the per-grant `deny_tool_risk`
    // constraint. Empty for surfaces/targets without risk metadata.
    tool_risk: &[String],
    grant: GrantRequirement,
) -> Result<Outcome, sqlx::Error> {
    // `expired`: the identity is past its `expires_at`,
    // judged on the database's clock in the statement that reads the
    // row, as the workload-token path does. Without it the key of an
    // agent whose identity had expired was served like a valid one.
    let agent: Option<(
        bool,
        bool,
        Option<chrono::DateTime<chrono::Utc>>,
        String,
        String,
        bool,
    )> = sqlx::query_as(
        "SELECT default_deny, enabled, revoked_at, status, enforcement_mode, \
                (expires_at IS NOT NULL AND expires_at <= now()) AS expired \
             FROM agent_identities WHERE id = $1 AND tenant_id = $2",
    )
    .bind(agent_id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await?;

    let (default_deny, enabled, revoked_at, status, enforcement_mode, expired) = match agent {
        Some(r) => r,
        // No identity row for this actor in the tenant it acts in:
        // the row was deleted under a live request, or the
        // actor is another tenant's agent (a shared key bound to an agent,
        // acting for a listed tenant). The gate cannot read what the agent
        // may do here, so it refuses; "no gate" would serve a default-deny
        // agent. Enforced: there is no row to read a dry-run mode from.
        None => {
            return Ok(Outcome {
                decision: Decision::Deny,
                matched_grant_id: None,
                reason: "agent identity not found in this tenant".to_string(),
                max_delegation_depth: None,
                dry_run: false,
                autonomy_level: None,
                autonomy_provenance: None,
            });
        }
    };
    let is_dry_run = enforcement_mode == "dry_run";
    if !enabled || revoked_at.is_some() || status != "active" || expired {
        return Ok(Outcome {
            decision: Decision::Deny,
            matched_grant_id: None,
            reason: "agent inactive or revoked".to_string(),
            max_delegation_depth: None,
            // A revoked credential is a hard security boundary — never dry-run.
            dry_run: false,
            // Denied before the autonomy gate; nothing was resolved.
            autonomy_level: None,
            autonomy_provenance: None,
        });
    }

    #[allow(clippy::type_complexity)]
    let rows: Vec<(
        String,
        String,
        String,
        String,
        bool,
        Option<chrono::DateTime<chrono::Utc>>,
        Option<serde_json::Value>,
    )> = sqlx::query_as(
        "SELECT id, action_type, target, effect, enabled, expires_at, constraints \
         FROM agent_grants WHERE tenant_id = $1 AND agent_id = $2 AND action_type = $3",
    )
    .bind(tenant_id)
    .bind(agent_id)
    .bind(action_type)
    .fetch_all(pool)
    .await?;

    let grants: Vec<Grant> = rows
        .into_iter()
        .map(|(id, at, tgt, eff, en, exp, cons)| {
            let effect = match eff.as_str() {
                "allow" => Decision::Allow,
                "deny" => Decision::Deny,
                "approval_required" => Decision::ApprovalRequired,
                // An effect this build does not know is the most
                // restrictive one: dropping the grant would leave the
                // resource group's decision in force.
                unknown => {
                    tracing::error!(
                        grant_id = %id,
                        effect = unknown,
                        "agent grant effect the gateway does not know; denying"
                    );
                    Decision::Deny
                }
            };
            Grant {
                id,
                action_type: at,
                target: tgt,
                effect,
                enabled: en,
                expires_at: exp.map(|d| d.timestamp()),
                constraints: GrantConstraints::from_json(cons.as_ref()),
            }
        })
        .collect();

    let now = chrono::Utc::now().timestamp();
    // Where no grant is required, the absence of one defers to the resource
    // group as it does for an agent that is not default-deny. A matching
    // grant, whatever its effect, is still the decision.
    let default_deny = default_deny && grant == GrantRequirement::Required;
    let mut outcome = decide(&grants, action_type, target, now, default_deny, rg_decision);
    // Stamp the agent's enforcement mode so the caller can shadow-log a
    // would-block instead of enforcing it.
    outcome.dry_run = is_dry_run;

    // Post-decision constraint gates on the matched grant. A violation
    // downgrades an Allow/ApprovalRequired to Deny (constraints can only
    // tighten, never widen). `Deny` outcomes are left untouched.
    if outcome.decision != Decision::Deny {
        if let Some(grant_id) = outcome.matched_grant_id.clone() {
            if let Some(grant) = grants.iter().find(|g| g.id == grant_id) {
                // (a) Max delegation depth — enforced when the caller supplied
                //     the request's depth (A2A). The signed group-level cap
                //     remains the authoritative depth control.
                if let (Some(cap), Some(depth)) =
                    (grant.constraints.max_delegation_depth, request_depth)
                {
                    if depth > cap {
                        outcome.decision = Decision::Deny;
                        outcome.reason = format!(
                            "agent grant constraint: delegation depth {depth} exceeds cap {cap}"
                        );
                        return Ok(outcome);
                    }
                }
                // (b) Tool risk class — deny when the target tool's risk
                //     attributes intersect the grant's deny list (e.g. block any
                //     destructive MCP tool).
                if let Some(deny) = &grant.constraints.deny_tool_risk {
                    if let Some(hit) = tool_risk.iter().find(|a| deny.contains(a)) {
                        outcome.decision = Decision::Deny;
                        outcome.reason =
                            format!("agent grant constraint: tool risk '{hit}' denied");
                        return Ok(outcome);
                    }
                }
                // (c) Rate limit — atomic check-and-increment per grant. Skipped
                //     when no usage_state is available (e.g. tests without Redis
                //     still exercise the in-memory counter when one is passed).
                if let (Some(rate), Some(us)) = (&grant.constraints.rate, usage_state) {
                    if rate.max > 0 {
                        let dim = crate::services::limits::window_count_dim(
                            "grant",
                            &format!("agentgrant:{}", grant.id),
                            rate_quota_window(&rate.window),
                            rate.max,
                            "grant_rate",
                            format!("agent grant {} rate", grant.id),
                        );
                        // Admitted atomically on the shared counters. A
                        // counter store that cannot be reached denies (fail
                        // closed) — it used to fall back to a per-pod count.
                        let admitted = crate::services::limits::admit(
                            us,
                            None,
                            vec![dim],
                            crate::services::cache::usage_state::Shape::count(),
                        )
                        .await;
                        let denied = match admitted {
                            Ok(Ok(_)) => None,
                            Ok(Err(_)) => Some(format!(
                                "agent grant constraint: rate limit {}/{} exceeded",
                                rate.max, rate.window
                            )),
                            Err(e) => {
                                tracing::error!(grant_id = %grant.id, error = %e, "grant rate unreadable; denying (fail closed)");
                                Some("agent grant constraint: rate limit cannot be checked right now".to_string())
                            }
                        };
                        if let Some(reason) = denied {
                            outcome.decision = Decision::Deny;
                            outcome.reason = reason;
                            return Ok(outcome);
                        }
                    }
                }
            }
        }
    }

    // (d) Autonomy. The last gate, and like the others it
    //     can only tighten. Resolved min-wins from the agent's own level and
    //     every group it belongs to, so an agent can never buy itself more
    //     freedom than its AI System allows.
    //
    //     Deliberately *not* subject to `dry_run`: `enforcement_mode` is
    //     about whether policy bites, and suspending a system is not a
    //     policy experiment. An operator who suspends a runaway agent and
    //     watches it keep acting because it happened to be in dry-run has
    //     been handed a control that does not work.
    if outcome.decision != Decision::Deny {
        let resolution =
            crate::services::autonomy::resolve_for_agent(pool, tenant_id, agent_id).await;
        let kind = crate::services::autonomy::classify(action_type, tool_risk);
        outcome.autonomy_level = Some(resolution.level.as_str());
        outcome.autonomy_provenance = Some(resolution.provenance.as_str());
        match crate::services::autonomy::decide_resolved(&resolution, kind) {
            crate::services::autonomy::Verdict::Allow => {}
            crate::services::autonomy::Verdict::RequireApproval => {
                if outcome.decision == Decision::Allow {
                    outcome.decision = Decision::ApprovalRequired;
                    outcome.reason = format!(
                        "autonomy level '{}': side-effecting calls need approval",
                        resolution.level.as_str()
                    );
                }
            }
            crate::services::autonomy::Verdict::Deny(message) => {
                outcome.decision = Decision::Deny;
                outcome.reason = message.to_string();
                outcome.dry_run = false;
            }
        }
    }

    Ok(outcome)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grant(id: &str, action: &str, target: &str, effect: Decision) -> Grant {
        Grant {
            id: id.into(),
            action_type: action.into(),
            target: target.into(),
            effect,
            enabled: true,
            expires_at: None,
            constraints: GrantConstraints::default(),
        }
    }

    /// Build a grant with explicit constraints (for the constraint tests).
    fn grant_with(
        id: &str,
        action: &str,
        target: &str,
        effect: Decision,
        constraints: GrantConstraints,
    ) -> Grant {
        Grant {
            constraints,
            ..grant(id, action, target, effect)
        }
    }

    #[test]
    fn allow_when_matching_allow_grant() {
        let g = vec![grant("g1", "llm_call", "gpt-5.2", Decision::Allow)];
        let o = decide(&g, "llm_call", "gpt-5.2", 0, true, None);
        assert_eq!(o.decision, Decision::Allow);
        assert_eq!(o.matched_grant_id.as_deref(), Some("g1"));
    }

    #[test]
    fn deny_wins_over_allow() {
        let g = vec![
            grant("a", "mcp_tool", "*", Decision::Allow),
            grant("d", "mcp_tool", "shell.*", Decision::Deny),
        ];
        let o = decide(&g, "mcp_tool", "shell.exec", 0, true, None);
        assert_eq!(o.decision, Decision::Deny);
        assert_eq!(o.matched_grant_id.as_deref(), Some("d"));
    }

    #[test]
    fn approval_beats_allow_below_deny() {
        let g = vec![
            grant("a", "a2a_call", "*", Decision::Allow),
            grant("ap", "a2a_call", "card-*", Decision::ApprovalRequired),
        ];
        let o = decide(&g, "a2a_call", "card-billing", 0, true, None);
        assert_eq!(o.decision, Decision::ApprovalRequired);
    }

    #[test]
    fn default_deny_when_no_grant() {
        let o = decide(&[], "llm_call", "gpt-5.2", 0, true, None);
        assert_eq!(o.decision, Decision::Deny);
        assert!(o.matched_grant_id.is_none());
    }

    #[test]
    fn no_grant_no_default_deny_defers_to_rg() {
        let o = decide(&[], "llm_call", "gpt-5.2", 0, false, Some(Decision::Allow));
        assert_eq!(o.decision, Decision::Allow);
        // and with no rg opinion either → allow
        let o2 = decide(&[], "llm_call", "gpt-5.2", 0, false, None);
        assert_eq!(o2.decision, Decision::Allow);
    }

    #[test]
    fn rg_stricter_than_agent_wins() {
        let g = vec![grant("a", "llm_call", "*", Decision::Allow)];
        let o = decide(&g, "llm_call", "gpt-5.2", 0, true, Some(Decision::Deny));
        assert_eq!(o.decision, Decision::Deny);
        // matched grant id suppressed because RG drove the decision
        assert!(o.matched_grant_id.is_none());
    }

    #[test]
    fn agent_stricter_than_rg_wins() {
        let g = vec![grant("d", "llm_call", "gpt-5.2", Decision::Deny)];
        let o = decide(&g, "llm_call", "gpt-5.2", 0, true, Some(Decision::Allow));
        assert_eq!(o.decision, Decision::Deny);
        assert_eq!(o.matched_grant_id.as_deref(), Some("d"));
    }

    #[test]
    fn expired_and_disabled_grants_ignored() {
        let mut expired = grant("e", "llm_call", "*", Decision::Allow);
        expired.expires_at = Some(100);
        let mut disabled = grant("x", "llm_call", "*", Decision::Allow);
        disabled.enabled = false;
        // now=200 > expiry, disabled inactive → no live grant → default-deny
        let o = decide(&[expired, disabled], "llm_call", "gpt-5.2", 200, true, None);
        assert_eq!(o.decision, Decision::Deny);
    }

    #[test]
    fn glob_matching() {
        assert!(glob_match("*", "anything"));
        assert!(glob_match("gpt-*", "gpt-5.2"));
        assert!(glob_match("*-mini", "gpt-5-mini"));
        assert!(glob_match("shell.*", "shell.exec"));
        assert!(glob_match("a*c", "abc"));
        assert!(glob_match("a*c", "axxxc"));
        assert!(!glob_match("gpt-*", "claude-3"));
        assert!(!glob_match("shell.*", "fs.read"));
        assert!(glob_match("exact", "exact"));
        assert!(!glob_match("exact", "exactly"));
    }

    #[test]
    fn action_type_scoping() {
        // an mcp_tool grant must not authorize an llm_call
        let g = vec![grant("m", "mcp_tool", "*", Decision::Allow)];
        let o = decide(&g, "llm_call", "gpt-5.2", 0, true, None);
        assert_eq!(o.decision, Decision::Deny); // default-deny, mcp grant doesn't apply
    }

    // --- Constraints -----------------------------------------------------

    use chrono::TimeZone;

    fn at_utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> chrono::DateTime<chrono::Utc> {
        chrono::Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
    }

    #[test]
    fn time_window_hours_inclusive_start_exclusive_end() {
        let tw = TimeWindowConstraint {
            days: None,
            start: Some("09:00".into()),
            end: Some("17:00".into()),
        };
        assert!(time_window_allows(&tw, at_utc(2026, 6, 29, 9, 0))); // start edge
        assert!(time_window_allows(&tw, at_utc(2026, 6, 29, 16, 59)));
        assert!(!time_window_allows(&tw, at_utc(2026, 6, 29, 17, 0))); // end edge excluded
        assert!(!time_window_allows(&tw, at_utc(2026, 6, 29, 8, 59)));
    }

    #[test]
    fn time_window_overnight_range_wraps_midnight() {
        let tw = TimeWindowConstraint {
            days: None,
            start: Some("22:00".into()),
            end: Some("02:00".into()),
        };
        assert!(time_window_allows(&tw, at_utc(2026, 6, 29, 23, 0)));
        assert!(time_window_allows(&tw, at_utc(2026, 6, 29, 1, 0)));
        assert!(!time_window_allows(&tw, at_utc(2026, 6, 29, 12, 0)));
    }

    #[test]
    fn time_window_days_filter() {
        // 2026-06-29 is a Monday (weekday from_sunday = 1).
        let tw = TimeWindowConstraint {
            days: Some(vec![1, 2, 3, 4, 5]), // Mon–Fri
            start: None,
            end: None,
        };
        assert!(time_window_allows(&tw, at_utc(2026, 6, 29, 12, 0))); // Mon
        assert!(!time_window_allows(&tw, at_utc(2026, 6, 28, 12, 0))); // Sun
    }

    #[test]
    fn grant_is_inert_outside_its_time_window() {
        // An allow grant gated 09:00–17:00 must not authorize at 20:00 → the
        // default-deny falls through.
        let tw = TimeWindowConstraint {
            days: None,
            start: Some("09:00".into()),
            end: Some("17:00".into()),
        };
        let g = vec![grant_with(
            "t",
            "llm_call",
            "*",
            Decision::Allow,
            GrantConstraints {
                time_window: Some(tw),
                ..Default::default()
            },
        )];
        let now_out = at_utc(2026, 6, 29, 20, 0).timestamp();
        let now_in = at_utc(2026, 6, 29, 12, 0).timestamp();
        assert_eq!(
            decide(&g, "llm_call", "gpt", now_out, true, None).decision,
            Decision::Deny
        );
        assert_eq!(
            decide(&g, "llm_call", "gpt", now_in, true, None).decision,
            Decision::Allow
        );
    }

    #[test]
    fn outcome_surfaces_matched_grant_depth_cap() {
        let g = vec![grant_with(
            "d",
            "a2a_call",
            "*",
            Decision::Allow,
            GrantConstraints {
                max_delegation_depth: Some(2),
                ..Default::default()
            },
        )];
        let o = decide(&g, "a2a_call", "card-x", 0, true, None);
        assert_eq!(o.decision, Decision::Allow);
        assert_eq!(o.max_delegation_depth, Some(2));
    }

    #[test]
    fn constraints_parse_from_json() {
        let v = serde_json::json!({
            "rate": {"max": 10, "window": "day"},
            "time_window": {"days": [1, 2], "start": "09:00", "end": "17:00"},
            "max_delegation_depth": 3
        });
        let c = GrantConstraints::from_json(Some(&v));
        assert_eq!(c.rate.as_ref().unwrap().max, 10);
        assert_eq!(c.rate.as_ref().unwrap().window, "day");
        assert_eq!(c.max_delegation_depth, Some(3));
        assert_eq!(c.time_window.as_ref().unwrap().days, Some(vec![1, 2]));
    }

    #[test]
    fn malformed_constraints_degrade_to_none() {
        let c = GrantConstraints::from_json(Some(&serde_json::json!("not-an-object")));
        assert!(c.rate.is_none() && c.time_window.is_none() && c.max_delegation_depth.is_none());
    }

    #[test]
    fn tool_risk_attributes_apply_mcp_spec_defaults() {
        // No annotations → conservative defaults (destructive + open_world).
        let none = tool_risk_attributes(None);
        assert!(none.contains(&"destructive".to_string()));
        assert!(none.contains(&"open_world".to_string()));
        assert!(!none.contains(&"read_only".to_string()));

        // read-only → not destructive, idempotent, (open_world default true).
        let ro = tool_risk_attributes(Some(&serde_json::json!({"readOnlyHint": true})));
        assert!(ro.contains(&"read_only".to_string()));
        assert!(ro.contains(&"idempotent".to_string()));
        assert!(!ro.contains(&"destructive".to_string()));

        // explicit non-destructive, closed-world → neither flagged.
        let safe = tool_risk_attributes(Some(&serde_json::json!({
            "destructiveHint": false, "openWorldHint": false
        })));
        assert!(!safe.contains(&"destructive".to_string()));
        assert!(!safe.contains(&"open_world".to_string()));

        // explicit destructive.
        let d = tool_risk_attributes(Some(&serde_json::json!({"destructiveHint": true})));
        assert!(d.contains(&"destructive".to_string()));
    }

    #[test]
    fn deny_tool_risk_parses_from_constraints() {
        let c = GrantConstraints::from_json(Some(&serde_json::json!({
            "deny_tool_risk": ["destructive", "open_world"]
        })));
        assert_eq!(
            c.deny_tool_risk,
            Some(vec!["destructive".to_string(), "open_world".to_string()])
        );
    }
}

/// Cross-plane contract: the tool objects the control plane stores (its
/// canonical normaliser's output) must drive the SAME runtime decisions the
/// fixture pins. The Python side asserts it produces `expected[*].stored`
/// from `tools_list_result`; this side asserts what the runtime makes of
/// those stored objects. Together they fail if either plane drops or
/// reinterprets annotations — the 2026-09 bug, where the legacy discovery
/// path whitelisted fields and every read-only tool became a write.
#[cfg(test)]
mod tool_annotation_contract {
    use super::tool_risk_attributes;
    use crate::services::autonomy::{CallKind, classify};
    use crate::services::evidence::attribution::read_only_map;
    use serde_json::{Value, json};

    fn fixture() -> Value {
        serde_json::from_str(include_str!(
            "../../tests/fixtures/mcp_tool_annotations_contract.json"
        ))
        .expect("contract fixture parses")
    }

    #[test]
    fn stored_annotations_drive_risk_autonomy_and_evidence() {
        let fx = fixture();
        let expected = fx["expected"].as_array().expect("expected[]");
        assert!(
            expected.len() >= 4,
            "fixture must cover annotated + unannotated tools"
        );
        let stored: Vec<Value> = expected.iter().map(|e| e["stored"].clone()).collect();
        let evidence = read_only_map(Some(&json!({ "tools": stored })));

        for case in expected {
            let tool = &case["stored"];
            let name = tool["name"].as_str().expect("name");
            let attrs = tool_risk_attributes(tool.get("annotations"));
            let want: Vec<String> = case["risk_attributes"]
                .as_array()
                .expect("risk_attributes")
                .iter()
                .map(|v| v.as_str().expect("attr").to_owned())
                .collect();
            assert_eq!(attrs, want, "risk attributes for {name}");

            let kind = classify("mcp_tool", &attrs);
            let want_kind = match case["call_kind"].as_str() {
                Some("read_only") => CallKind::ReadOnly,
                Some("side_effecting") => CallKind::SideEffecting,
                other => panic!("bad call_kind {other:?} for {name}"),
            };
            assert_eq!(kind, want_kind, "autonomy classification for {name}");

            assert_eq!(
                evidence.get(name).copied(),
                case["evidence_read_only"].as_bool(),
                "evidence read-only attribution for {name}"
            );
        }
    }

    #[test]
    fn an_unannotated_tool_still_fails_closed() {
        // The fix must not weaken the rule: no annotations = side-effecting.
        assert_eq!(
            classify("mcp_tool", &tool_risk_attributes(None)),
            CallKind::SideEffecting
        );
        let fx = fixture();
        let unannotated = fx["expected"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["stored"].get("annotations").is_none())
            .expect("fixture carries an unannotated tool");
        assert_eq!(unannotated["call_kind"], "side_effecting");
    }
}
