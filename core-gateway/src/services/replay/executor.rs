// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Executing a replay.
//!
//! ## The correction: the agent loop is not ours to run
//!
//! The RFC describes replaying "the recorded root request through the real
//! gateway path" and mocking side effects, as though re-issuing the first
//! request would reproduce the whole trajectory. It would not, and the
//! reason is structural rather than an implementation gap.
//!
//! A run is an end-to-end *task*: the customer's agent framework decides to
//! call a model, reads the answer, decides to call a tool, and loops. That
//! loop lives in **their** process. The gateway sees each step and stamps a
//! signed chain across them, but it never drives the iteration. Re-issuing
//! the root request would replay exactly one step and then stop, and the
//! resulting "trajectory" — one step, no tools — would differ from every
//! recorded run for reasons that have nothing to do with the change being
//! tested. A gate built on that would fail everything, always.
//!
//! ## What replay does instead, and why it is the better question
//!
//! It re-evaluates **every recorded step's authorization decision** under the
//! candidate contract, without dispatching anything, and diffs the verdicts
//! against what actually happened.
//!
//! That answers precisely the question the promotion gate asks — *does this
//! contract govern this traffic differently?* — and it answers it better
//! than trajectory replay would:
//!
//! * **It cannot move money.** Not by policy, not by a default someone might
//!   flip: nothing is dispatched upstream at all. `side_effect_policy` is
//!   preserved on the case for the day full replay exists, and `skip_case`
//!   is still honoured, but the safety property here is structural.
//! * **It is deterministic.** Two replays of the same suite against the same
//!   contract produce identical verdicts, so a failing gate is reproducible
//!   rather than a coin flip against a live provider.
//! * **It is free.** No tokens, no provider latency. A suite of thirty cases
//!   with four hundred steps runs in the time of four hundred SQL-backed
//!   decisions.
//! * **It is also the dry-run simulation**, which is the
//!   same computation asked from the other direction.
//!
//! ## What it therefore cannot tell you
//!
//! Whether the agent would have *behaved* differently — taken another route,
//! looped more, produced a worse answer. Behavioural change is what
//! drift detection is for, on real traffic, after the fact. And content
//! guardrails are not re-evaluated: whether a PII check would trip on this
//! request depends on running it, and a replay must not imply it ran a
//! check we did not run. A guardrail that has been *unbound* from the system
//! is a composition change and is caught by the contract diff.
//!
//! This diffs **authorization**, not truth.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use sqlx::{FromRow, PgPool};

use crate::services::autonomy::{self, CallKind};
use crate::services::replay::diff::{self, CaseDiff, Trajectory};

/// One step as it was recorded, reduced to the facts authorization depends on.
#[derive(Debug, Clone, FromRow)]
pub struct RecordedAction {
    pub proxy_type: String,
    pub llm_model_requested: Option<String>,
    pub mcp_capability_name: Option<String>,
    pub server_id: Option<String>,
    pub actor_agent_id: Option<String>,
    pub success: Option<bool>,
    pub guardrail_blocked: Option<bool>,
    pub argument_policy_denied: Option<bool>,
    pub agent_authz_decision: Option<String>,
}

impl RecordedAction {
    /// Whether the gateway let this step through when it actually happened.
    pub fn was_allowed(&self) -> bool {
        self.success.unwrap_or(false)
            && !self.guardrail_blocked.unwrap_or(false)
            && !self.argument_policy_denied.unwrap_or(false)
            && self.agent_authz_decision.as_deref() != Some("deny")
    }

    fn call_kind(&self) -> CallKind {
        // Mirrors `autonomy::classify`, but from a recorded step's proxy
        // type rather than a live action string.
        match self.proxy_type.as_str() {
            "llm" => CallKind::ReadOnly,
            _ => CallKind::SideEffecting,
        }
    }

    fn action_type(&self) -> &'static str {
        match self.proxy_type.as_str() {
            "llm" => "llm_call",
            "skill" => "skill_exec",
            "a2a" => "a2a_call",
            _ => "mcp_tool",
        }
    }

    fn target(&self) -> String {
        self.mcp_capability_name
            .clone()
            .or_else(|| self.llm_model_requested.clone())
            .unwrap_or_else(|| "*".to_string())
    }
}

/// What the candidate contract would do with a step.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ActionVerdict {
    Allowed,
    ApprovalRequired(String),
    Denied(String),
}

impl ActionVerdict {
    pub fn is_allowed(&self) -> bool {
        matches!(self, Self::Allowed)
    }
}

const STEPS_SQL: &str = r#"
SELECT
    pl.proxy_type,
    pl.llm_model_requested,
    pl.mcp_capability_name,
    pl.server_id,
    pl.actor_agent_id,
    pl.success,
    pl.guardrail_blocked,
    pl.argument_policy_denied,
    pl.agent_authz_decision
FROM proxy_logs pl
WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
ORDER BY pl.timestamp ASC, pl.id ASC
"#;

/// A database error is returned: an empty step list reads as "aged out
/// by retention" and the case would be recorded as failed for that reason.
pub async fn load_actions(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Vec<RecordedAction>, sqlx::Error> {
    sqlx::query_as::<_, RecordedAction>(STEPS_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .fetch_all(db)
        .await
}

/// Is this model still reachable from the AI System's resolved closure?
///
/// Walks the inheritance chain exactly as the contract closure does, so a
/// replay and the contract cannot disagree about what is bound.
async fn model_is_bound(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    model_name: &str,
) -> Result<bool, sqlx::Error> {
    const SQL: &str = r#"
WITH RECURSIVE chain AS (
    SELECT id, parent_group_id, inherit_from_parent, 0 AS depth
      FROM resource_groups WHERE id = $2 AND tenant_id = $1
    UNION ALL
    SELECT p.id, p.parent_group_id, p.inherit_from_parent, c.depth + 1
      FROM chain c JOIN resource_groups p ON p.id = c.parent_group_id
     WHERE c.inherit_from_parent = true AND c.depth < 10
)
SELECT EXISTS (
    SELECT 1
      FROM resource_group_llm_models b
      JOIN llm_model m ON m.id = b.llm_model_id
     WHERE b.resource_group_id IN (SELECT id FROM chain)
       AND (m.name = $3 OR m.model_name = $3)
)
"#;
    sqlx::query_scalar::<_, bool>(SQL)
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(model_name)
        .fetch_one(db)
        .await
    // Nor an allowance: a failed lookup is returned, so the replay
    // errors (and is retried) instead of passing or failing on a guess.
}

async fn server_is_bound(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    server_config_id: &str,
) -> Result<bool, sqlx::Error> {
    const SQL: &str = r#"
WITH RECURSIVE chain AS (
    SELECT id, parent_group_id, inherit_from_parent, 0 AS depth
      FROM resource_groups WHERE id = $2 AND tenant_id = $1
    UNION ALL
    SELECT p.id, p.parent_group_id, p.inherit_from_parent, c.depth + 1
      FROM chain c JOIN resource_groups p ON p.id = c.parent_group_id
     WHERE c.inherit_from_parent = true AND c.depth < 10
)
SELECT EXISTS (
    SELECT 1 FROM resource_group_server_configs b
     WHERE b.resource_group_id IN (SELECT id FROM chain)
       AND b.server_config_id = $3
)
"#;
    sqlx::query_scalar::<_, bool>(SQL)
        .bind(tenant_id)
        .bind(ai_system_id)
        .bind(server_config_id)
        .fetch_one(db)
        .await
}

/// Re-decide one step under current configuration. Dispatches nothing.
pub async fn evaluate_action(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    step: &RecordedAction,
) -> Result<ActionVerdict, sqlx::Error> {
    // 1. Composition — is the resource still reachable at all? This is the
    //    change a contract diff reports as "lost an MCP server", and it is
    //    the one most likely to break a working system.
    if step.proxy_type == "llm" {
        if let Some(model) = step.llm_model_requested.as_deref() {
            if !model_is_bound(db, tenant_id, ai_system_id, model).await? {
                return Ok(ActionVerdict::Denied(format!(
                    "model {model} is no longer bound to this AI System"
                )));
            }
        }
    } else if let Some(server) = step.server_id.as_deref() {
        if !server_is_bound(db, tenant_id, ai_system_id, server).await? {
            return Ok(ActionVerdict::Denied(format!(
                "MCP server {server} is no longer bound to this AI System"
            )));
        }
    }

    // 2. Agent authorization, which also applies the autonomy gate. Only
    //    meaningful when the step had an agent actor.
    if let Some(agent_id) = step.actor_agent_id.as_deref() {
        let tool_risk: Vec<String> = Vec::new();
        let outcome = crate::services::agent_authz::decide_for_agent(
            db,
            tenant_id,
            agent_id,
            step.action_type(),
            &step.target(),
            None,
            None,
            None,
            &tool_risk,
        )
        .await?;
        // A failed query is returned. The gate always decides: an
        // actor with no identity row in the tenant is denied.
        match outcome.decision {
            crate::services::agent_authz::Decision::Deny => {
                return Ok(ActionVerdict::Denied(outcome.reason));
            }
            crate::services::agent_authz::Decision::ApprovalRequired => {
                return Ok(ActionVerdict::ApprovalRequired(outcome.reason));
            }
            crate::services::agent_authz::Decision::Allow => {}
        }
    } else {
        // 3. No agent actor — the AI System's own autonomy still applies.
        let level = system_autonomy(db, tenant_id, ai_system_id).await?;
        match autonomy::decide(level, step.call_kind()) {
            autonomy::Verdict::Allow => {}
            autonomy::Verdict::RequireApproval => {
                return Ok(ActionVerdict::ApprovalRequired(format!(
                    "autonomy level '{}': side-effecting calls need approval",
                    level.as_str()
                )));
            }
            autonomy::Verdict::Deny(message) => {
                return Ok(ActionVerdict::Denied(message.to_string()));
            }
        }
    }

    Ok(ActionVerdict::Allowed)
}

/// A failed read is returned, never the default level.
async fn system_autonomy(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
) -> Result<autonomy::Autonomy, sqlx::Error> {
    const SQL: &str = r#"
WITH RECURSIVE chain AS (
    SELECT id, parent_group_id, inherit_from_parent, autonomy_level, 0 AS depth
      FROM resource_groups WHERE id = $2 AND tenant_id = $1
    UNION ALL
    SELECT p.id, p.parent_group_id, p.inherit_from_parent, p.autonomy_level, c.depth + 1
      FROM chain c JOIN resource_groups p ON p.id = c.parent_group_id
     WHERE c.inherit_from_parent = true AND c.depth < 10
)
SELECT autonomy_level FROM chain WHERE autonomy_level IS NOT NULL
"#;
    match sqlx::query_as::<_, (String,)>(SQL)
        .bind(tenant_id)
        .bind(ai_system_id)
        .fetch_all(db)
        .await
    {
        Ok(rows) => Ok(autonomy::resolve(rows.iter().map(|(l,)| Some(l.as_str())))),
        Err(e) => Err(e),
    }
}

/// The outcome of replaying one case.
#[derive(Debug, Clone, Serialize)]
pub struct CaseOutcome {
    pub case_id: String,
    pub is_critical: bool,
    pub diff: CaseDiff,
    /// Per-step detail, so an operator can see *which* step changed rather
    /// than only that something did.
    pub changed_actions: Vec<ChangedAction>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChangedAction {
    pub index: usize,
    pub target: String,
    pub was_allowed: bool,
    pub verdict: ActionVerdict,
}

/// Replay one case: re-decide every recorded step, then diff.
///
/// The recorded trajectory comes from the frozen case; the observed one is
/// synthesised from the verdicts. Step count and tool signature are carried
/// across unchanged — nothing here can alter them, and reporting a spurious
/// shape change would bury the governance finding that is the point.
pub async fn replay_case(
    db: &PgPool,
    tenant_id: &str,
    ai_system_id: &str,
    case_id: &str,
    root_task_id: Option<&str>,
    recorded: &Trajectory,
    is_critical: bool,
    side_effect_policy: &str,
) -> Result<CaseOutcome, sqlx::Error> {
    if side_effect_policy == super::side_effect::SKIP {
        return Ok(CaseOutcome {
            case_id: case_id.to_string(),
            is_critical,
            diff: diff::skipped(
                recorded.clone(),
                "case is marked skip_case: its side effects cannot be made safe",
            ),
            changed_actions: Vec::new(),
        });
    }

    let Some(root) = root_task_id else {
        return Ok(CaseOutcome {
            case_id: case_id.to_string(),
            is_critical,
            diff: diff::failed(
                recorded.clone(),
                "case has no recorded root task; its steps cannot be found",
            ),
            changed_actions: Vec::new(),
        });
    };

    let steps = load_actions(db, tenant_id, root).await?;
    if steps.is_empty() {
        // Retention has aged the steps out. Reported as a failure rather
        // than a pass: a case that cannot be evaluated is not evidence.
        return Ok(CaseOutcome {
            case_id: case_id.to_string(),
            is_critical,
            diff: diff::failed(
                recorded.clone(),
                "recorded steps are no longer available (retention); rebuild the suite",
            ),
            changed_actions: Vec::new(),
        });
    }

    let mut denied = 0i32;
    let mut approvals = 0i32;
    let mut newly_denied = 0i32;
    let mut changed_actions = Vec::new();

    for (index, step) in steps.iter().enumerate() {
        let verdict = evaluate_action(db, tenant_id, ai_system_id, step).await?;
        let was_allowed = step.was_allowed();

        match &verdict {
            ActionVerdict::Denied(_) => {
                denied += 1;
                if was_allowed {
                    newly_denied += 1;
                }
            }
            ActionVerdict::ApprovalRequired(_) => approvals += 1,
            ActionVerdict::Allowed => {}
        }

        if verdict.is_allowed() != was_allowed
            || matches!(verdict, ActionVerdict::ApprovalRequired(_))
        {
            changed_actions.push(ChangedAction {
                index,
                target: step.target(),
                was_allowed,
                verdict,
            });
        }
    }

    // A step that used to succeed and is now denied means the task would not
    // have completed. That is an outcome change, not a policy nuance, and
    // the gate must treat it as one.
    // A run the gateway stops is `errored` (cause
    // `blocked_by_policy`); `blocked_policy` is no longer a terminal state.
    let observed_terminal = if newly_denied > 0 {
        Some(crate::services::run_ledger::terminal::ERRORED.to_string())
    } else {
        recorded.terminal_state.clone()
    };

    let observed = Trajectory {
        terminal_state: observed_terminal,
        // Carried across: authorization replay cannot change the shape, and
        // a spurious shape change would bury the governance finding.
        action_count: recorded.action_count,
        tool_set_signature: recorded.tool_set_signature.clone(),
        total_cost_usd: recorded.total_cost_usd,
        // Not re-evaluated — see the module docs. Carried so it never
        // produces a difference we did not actually observe.
        guardrail_block_count: recorded.guardrail_block_count,
        policy_denial_count: Some(denied),
        approval_request_count: Some(approvals),
    };

    Ok(CaseOutcome {
        case_id: case_id.to_string(),
        is_critical,
        diff: diff::compare(recorded, &observed),
        changed_actions,
    })
}

/// Roll case outcomes into the counters a replay run stores.
pub fn summarize_outcomes(outcomes: &[CaseOutcome]) -> diff::Summary {
    let pairs: Vec<(CaseDiff, bool)> = outcomes
        .iter()
        .map(|o| (o.diff.clone(), o.is_critical))
        .collect();
    diff::summarize(&pairs)
}

/// Per-case results in the shape `ai_system_replay_runs.case_results` holds.
pub fn case_results_json(outcomes: &[CaseOutcome]) -> serde_json::Value {
    serde_json::Value::Array(
        outcomes
            .iter()
            .map(|o| {
                let mut m = serde_json::Map::new();
                m.insert("case_id".into(), o.case_id.clone().into());
                m.insert("verdict".into(), o.diff.verdict.into());
                m.insert("is_critical".into(), o.is_critical.into());
                m.insert(
                    "reasons".into(),
                    serde_json::Value::Array(
                        o.diff.reasons.iter().map(|r| r.clone().into()).collect(),
                    ),
                );
                m.insert(
                    "changed_actions".into(),
                    serde_json::to_value(&o.changed_actions).unwrap_or(serde_json::Value::Null),
                );
                serde_json::Value::Object(m)
            })
            .collect(),
    )
}

/// Turn a stored `recorded_trajectory` blob into a [`Trajectory`].
pub fn trajectory_from_json(value: &serde_json::Value) -> Trajectory {
    let get_i32 = |k: &str| value.get(k).and_then(|v| v.as_i64()).map(|v| v as i32);
    Trajectory {
        terminal_state: value
            .get("terminal_state")
            .and_then(|v| v.as_str())
            .map(String::from),
        action_count: get_i32("action_count"),
        tool_set_signature: value
            .get("tool_set_signature")
            .and_then(|v| v.as_str())
            .map(String::from),
        total_cost_usd: value.get("total_cost_usd").and_then(|v| v.as_f64()),
        guardrail_block_count: get_i32("guardrail_block_count"),
        policy_denial_count: get_i32("policy_denial_count"),
        approval_request_count: get_i32("approval_request_count"),
    }
}

/// Coverage of a suite by stratum, for the operator-facing summary.
pub fn coverage(strata: &[Option<String>]) -> BTreeMap<String, usize> {
    let mut out = BTreeMap::new();
    for s in strata {
        *out.entry(s.clone().unwrap_or_else(|| "unlabelled".into()))
            .or_insert(0) += 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step(proxy: &str, allowed: bool) -> RecordedAction {
        RecordedAction {
            proxy_type: proxy.into(),
            llm_model_requested: Some("gpt-4o".into()),
            mcp_capability_name: if proxy == "mcp" {
                Some("search".into())
            } else {
                None
            },
            server_id: if proxy == "mcp" {
                Some("srv-1".into())
            } else {
                None
            },
            actor_agent_id: None,
            success: Some(allowed),
            guardrail_blocked: Some(false),
            argument_policy_denied: Some(false),
            agent_authz_decision: None,
        }
    }

    #[test]
    fn a_successful_action_reads_as_allowed() {
        assert!(step("llm", true).was_allowed());
    }

    #[test]
    fn a_guardrail_block_is_not_allowed_even_when_the_call_succeeded() {
        // `success` is about the HTTP exchange; a blocked call can still be
        // a 200 to the client with a refusal body.
        let mut h = step("llm", true);
        h.guardrail_blocked = Some(true);
        assert!(!h.was_allowed());
    }

    #[test]
    fn an_argument_policy_denial_is_not_allowed() {
        let mut h = step("mcp", true);
        h.argument_policy_denied = Some(true);
        assert!(!h.was_allowed());
    }

    #[test]
    fn an_agent_deny_is_not_allowed() {
        let mut h = step("mcp", true);
        h.agent_authz_decision = Some("deny".into());
        assert!(!h.was_allowed());
    }

    #[test]
    fn a_missing_success_flag_is_not_treated_as_success() {
        // Absent evidence must not read as a pass.
        let mut h = step("llm", true);
        h.success = None;
        assert!(!h.was_allowed());
    }

    #[test]
    fn completions_are_read_only_and_tools_are_not() {
        assert_eq!(step("llm", true).call_kind(), CallKind::ReadOnly);
        assert_eq!(step("mcp", true).call_kind(), CallKind::SideEffecting);
        assert_eq!(step("skill", true).call_kind(), CallKind::SideEffecting);
    }

    #[test]
    fn action_types_match_the_authz_vocabulary() {
        assert_eq!(step("llm", true).action_type(), "llm_call");
        assert_eq!(step("mcp", true).action_type(), "mcp_tool");
        assert_eq!(step("skill", true).action_type(), "skill_exec");
        assert_eq!(step("a2a", true).action_type(), "a2a_call");
    }

    #[test]
    fn the_target_prefers_the_tool_over_the_model() {
        assert_eq!(step("mcp", true).target(), "search");
        assert_eq!(step("llm", true).target(), "gpt-4o");
    }

    #[test]
    fn trajectory_round_trips_through_json() {
        let t = Trajectory {
            terminal_state: Some("completed".into()),
            action_count: Some(7),
            tool_set_signature: Some("abc".into()),
            total_cost_usd: Some(0.02),
            guardrail_block_count: Some(1),
            policy_denial_count: Some(0),
            approval_request_count: Some(2),
        };
        let json = serde_json::to_value(&t).unwrap();
        assert_eq!(trajectory_from_json(&json), t);
    }

    #[test]
    fn a_missing_field_becomes_none_rather_than_zero() {
        // Zero and "not recorded" are different facts; conflating them
        // would manufacture diffs against sparse older cases.
        let t = trajectory_from_json(&serde_json::json!({"terminal_state": "completed"}));
        assert_eq!(t.action_count, None);
        assert_eq!(t.policy_denial_count, None);
    }

    #[test]
    fn coverage_counts_by_stratum() {
        let c = coverage(&[
            Some("terminal:completed".into()),
            Some("terminal:completed".into()),
            None,
        ]);
        assert_eq!(c["terminal:completed"], 2);
        assert_eq!(c["unlabelled"], 1);
    }

    #[test]
    fn case_results_carry_the_reasons_not_just_the_verdict() {
        let recorded = Trajectory {
            terminal_state: Some("completed".into()),
            policy_denial_count: Some(0),
            ..Default::default()
        };
        let observed = Trajectory {
            terminal_state: Some("errored".into()),
            policy_denial_count: Some(1),
            ..Default::default()
        };
        let outcome = CaseOutcome {
            case_id: "c1".into(),
            is_critical: true,
            diff: diff::compare(&recorded, &observed),
            changed_actions: vec![ChangedAction {
                index: 3,
                target: "transfer_funds".into(),
                was_allowed: true,
                verdict: ActionVerdict::Denied("no longer bound".into()),
            }],
        };
        let json = case_results_json(&[outcome]);
        let first = &json[0];
        assert_eq!(first["verdict"], "outcome_changed");
        assert_eq!(first["is_critical"], true);
        assert!(!first["reasons"].as_array().unwrap().is_empty());
        // The *which step* detail is what turns a failing gate into an
        // actionable one.
        assert_eq!(first["changed_actions"][0]["target"], "transfer_funds");
    }

    #[test]
    fn summarizing_outcomes_matches_the_diff_summary() {
        let t = Trajectory {
            terminal_state: Some("completed".into()),
            ..Default::default()
        };
        let outcomes = vec![
            CaseOutcome {
                case_id: "a".into(),
                is_critical: false,
                diff: diff::compare(&t, &t),
                changed_actions: vec![],
            },
            CaseOutcome {
                case_id: "b".into(),
                is_critical: true,
                diff: diff::compare(
                    &t,
                    &Trajectory {
                        terminal_state: Some("errored".into()),
                        ..Default::default()
                    },
                ),
                changed_actions: vec![],
            },
        ];
        let s = summarize_outcomes(&outcomes);
        assert_eq!(s.matched, 1);
        assert_eq!(s.outcome_changed, 1);
        assert_eq!(s.critical_outcome_changed, 1);
        assert_eq!(s.pass_rate, Some(0.5));
    }
}
