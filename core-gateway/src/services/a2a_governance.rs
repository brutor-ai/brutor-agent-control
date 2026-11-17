// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A policy: ONE evaluation for every A2A route of both protocol versions.
//!
//! A policy document can be set in three places, and all of them apply to a
//! call:
//!
//! * the caller's resource group (`resource_groups.a2a_global_governance`),
//! * every ancestor of that group, up to the root (the root's document is
//!   the tenant's; it is also what a caller with no group is judged by),
//! * the agent card the call addresses (`agent_cards.tenant_governance`).
//!
//! The document:
//!
//! ```json
//! {
//!   "capability": {"default_access": "enabled|approval_required|disabled",
//!                  "approval_timeout_seconds": 900},
//!   "data":       {"deny_classifications": ["PII", "PHI"]},
//!   "remote":     {"allowed_globs": ["https://*.partner.example/*"]}
//! }
//! ```
//!
//! ## The strictest level wins
//!
//! Each level is judged on its own and the strictest verdict is the call's:
//! a refusal anywhere refuses, else a hold anywhere holds, else the call is
//! allowed. Nothing is merged into one document first. A merge of two
//! allow-lists by the patterns they share is empty when they share none, and
//! an empty list restricts nothing: two levels that each restricted the call
//! used to cancel each other out. Judged per level, the address has to match
//! the list of every level that states one.
//!
//! ## What each control says
//!
//! * `default_access: disabled` refuses every A2A call, reading verbs
//!   included. `approval_required` holds a call that does work (a message,
//!   an invocation) for a person through the approval queue
//!   (`agent_approval::require_or_enqueue`); a call that only reads is not
//!   held. A value that is not one of the three refuses.
//! * `deny_classifications` refuses a call judged by a capability that
//!   carries one of the tags (compared without case).
//! * `allowed_globs` is about the address an OUTBOUND call leaves for. An
//!   inbound call has no remote address, so the list does not judge it. A
//!   non-empty list that names no readable pattern allows nothing.
//!
//! A document, or a part of one, that cannot be read refuses (fail closed).
//!
//! ## One place
//!
//! Every A2A route calls [`enforce`]; `tests/a2a_governance_guard.rs` refuses
//! a route that does not, and a second evaluation anywhere else.

use axum::http::StatusCode;
use globset::Glob;
use serde_json::Value;
use sqlx::PgPool;

use crate::models::auth::AuthContext;
use crate::services::SharedState;
use crate::services::agent_approval;
use crate::services::agent_authz;
use crate::services::autonomy;
use crate::services::cache::EffectiveGroupData;
use crate::services::run_controls::RunScope;

/// The action an A2A call is, to an agent's grants, to the autonomy gate and
/// on an approval.
const ACTION: &str = "a2a_call";

/// The message a call is refused with when its policy cannot be loaded.
pub const UNAVAILABLE_MESSAGE: &str =
    "A2A policy is unavailable; refusing the call until it can be read";

/// Where a policy document is set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// A resource group: the caller's own, an ancestor, or the tenant root.
    Group,
    /// The agent card the call addresses.
    Card,
}

impl Source {
    fn names(self) -> &'static str {
        match self {
            Source::Group => "Group governance",
            Source::Card => "Agent card policy",
        }
    }

    fn allow_list(self) -> &'static str {
        match self {
            Source::Group => "the group's allow-list",
            Source::Card => "the agent card's allow-list",
        }
    }
}

/// Every policy document that applies to one call, each judged on its own.
#[derive(Debug, Clone, Default)]
pub struct Policy {
    levels: Vec<(Source, Value)>,
}

impl Policy {
    /// A policy of the given documents (`null` documents state nothing).
    pub fn of(levels: impl IntoIterator<Item = (Source, Value)>) -> Self {
        Self {
            levels: levels.into_iter().filter(|(_, v)| !v.is_null()).collect(),
        }
    }

    /// Whether no level states anything.
    pub fn is_empty(&self) -> bool {
        self.levels.is_empty()
    }

    /// The verdict on `subject`: the strictest any level gives.
    pub fn judge(&self, subject: &Subject<'_>) -> Verdict {
        let mut hold = false;
        let mut window: Option<i32> = None;
        for (source, doc) in &self.levels {
            match judge_level(*source, doc, subject) {
                Level::Refuse(reason) => return Verdict::Refuse(reason),
                Level::Hold => hold = true,
                Level::Allow => {}
            }
            // The shortest window any level states is the hold's.
            if let Some(w) = stated_window(doc) {
                window = Some(window.map_or(w, |cur| cur.min(w)));
            }
        }
        if hold {
            Verdict::Hold {
                window_seconds: window,
            }
        } else {
            Verdict::Allow
        }
    }
}

/// Which way the call goes.
#[derive(Debug, Clone, Copy)]
pub enum Direction<'a> {
    /// The call leaves for a remote agent at this address.
    Outbound { remote_url: &'a str },
    /// The call arrives for an agent this gateway serves: no remote address.
    Inbound,
}

/// What a policy is asked about.
#[derive(Debug, Clone)]
pub struct Subject<'a> {
    pub direction: Direction<'a>,
    /// `data_classification` of every capability the call is judged by (the
    /// one it names or is dispatched to). Empty for a call that addresses no
    /// capability (a reading verb).
    pub classifications: Vec<&'a Value>,
    /// Whether the call does work (a message, an invocation). Only work is
    /// held for approval; `disabled` refuses a reading call as well.
    pub work: bool,
}

/// A policy's verdict on one call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    /// Held for a person; `window_seconds` is the window the policy states.
    Hold {
        window_seconds: Option<i32>,
    },
    Refuse(String),
}

enum Level {
    Allow,
    Hold,
    Refuse(String),
}

fn stated_window(doc: &Value) -> Option<i32> {
    doc.get("capability")
        .and_then(|c| c.get("approval_timeout_seconds"))
        .and_then(Value::as_i64)
        .filter(|w| *w > 0)
        .map(|w| i32::try_from(w).unwrap_or(i32::MAX))
}

fn unreadable(source: Source, what: &str) -> Level {
    Level::Refuse(format!(
        "{} cannot be read ({what}); refusing the call",
        source.names()
    ))
}

/// The tags of one capability's `data_classification`: a list of strings, or
/// one string. `None` when it is neither (it cannot be read).
fn tags(classification: &Value) -> Option<Vec<&str>> {
    match classification {
        Value::Null => Some(Vec::new()),
        Value::String(s) => Some(vec![s.as_str()]),
        Value::Array(items) => items.iter().map(Value::as_str).collect(),
        _ => None,
    }
}

fn judge_level(source: Source, doc: &Value, subject: &Subject<'_>) -> Level {
    let Value::Object(doc) = doc else {
        return unreadable(source, "the policy is not an object");
    };
    let mut hold = false;

    // capability.default_access
    match doc.get("capability") {
        None | Some(Value::Null) => {}
        Some(Value::Object(cap)) => match cap.get("default_access") {
            None | Some(Value::Null) => {}
            Some(Value::String(access)) => match access.as_str() {
                "enabled" => {}
                "disabled" => {
                    return Level::Refuse(format!(
                        "{} disables this capability (default_access=disabled)",
                        source.names()
                    ));
                }
                "approval_required" => hold = subject.work,
                other => {
                    return Level::Refuse(format!(
                        "{} states an access the gateway does not know \
                         (default_access={other}); refusing the call",
                        source.names()
                    ));
                }
            },
            Some(_) => return unreadable(source, "capability.default_access is not a string"),
        },
        Some(_) => return unreadable(source, "capability is not an object"),
    }

    // data.deny_classifications
    match doc.get("data") {
        None | Some(Value::Null) => {}
        Some(Value::Object(data)) => match data.get("deny_classifications") {
            None | Some(Value::Null) => {}
            Some(Value::Array(deny)) => {
                let Some(deny) = deny
                    .iter()
                    .map(Value::as_str)
                    .collect::<Option<Vec<&str>>>()
                else {
                    return unreadable(source, "data.deny_classifications is not a list of tags");
                };
                if !deny.is_empty() {
                    for classification in &subject.classifications {
                        let Some(tags) = tags(classification) else {
                            return Level::Refuse(format!(
                                "{} denies data classifications and the capability's \
                                 classification cannot be read; refusing the call",
                                source.names()
                            ));
                        };
                        for tag in tags {
                            if deny.iter().any(|d| d.eq_ignore_ascii_case(tag)) {
                                return Level::Refuse(format!(
                                    "{} denies data classification `{tag}`",
                                    source.names()
                                ));
                            }
                        }
                    }
                }
            }
            Some(_) => return unreadable(source, "data.deny_classifications is not a list"),
        },
        Some(_) => return unreadable(source, "data is not an object"),
    }

    // remote.allowed_globs: the address an outbound call leaves for.
    if let Direction::Outbound { remote_url } = subject.direction {
        match doc.get("remote") {
            None | Some(Value::Null) => {}
            Some(Value::Object(remote)) => match remote.get("allowed_globs") {
                None | Some(Value::Null) => {}
                Some(Value::Array(globs)) => {
                    // A pattern that cannot be read matches nothing, so a
                    // list of only such patterns allows nothing.
                    let allowed = globs.is_empty()
                        || globs
                            .iter()
                            .filter_map(Value::as_str)
                            .filter_map(|p| Glob::new(p).ok())
                            .any(|g| g.compile_matcher().is_match(remote_url));
                    if !allowed {
                        return Level::Refuse(format!(
                            "Remote URL `{remote_url}` is not in {}",
                            source.allow_list()
                        ));
                    }
                }
                Some(_) => return unreadable(source, "remote.allowed_globs is not a list"),
            },
            Some(_) => return unreadable(source, "remote is not an object"),
        }
    }

    if hold { Level::Hold } else { Level::Allow }
}

/// The tenant's own policy: the document of each root resource group. It is
/// what judges a caller that has no group (a caller with one reaches it
/// through its chain). An error is returned, never read as "no policy".
pub async fn tenant_root_policies(db: &PgPool, tenant_id: &str) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Option<Value>,)> = sqlx::query_as(
        "SELECT a2a_global_governance FROM resource_groups \
         WHERE tenant_id = $1 AND parent_group_id IS NULL AND is_active = true",
    )
    .bind(tenant_id)
    .fetch_all(db)
    .await?;
    Ok(rows
        .into_iter()
        .filter_map(|(g,)| g)
        .filter(|g| !g.is_null())
        .collect())
}

/// Every policy document that applies to a call by a caller of `group` (its
/// chain; the tenant's roots when it has none) to the card whose own policy
/// is `card_policy`.
pub async fn policy_for(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&EffectiveGroupData>,
    card_policy: Option<&Value>,
) -> Result<Policy, sqlx::Error> {
    let group_levels = match group {
        Some(g) => g.a2a_governance_levels.clone(),
        None => tenant_root_policies(db, tenant_id).await?,
    };
    Ok(Policy::of(
        group_levels
            .into_iter()
            .map(|doc| (Source::Group, doc))
            .chain(card_policy.cloned().map(|doc| (Source::Card, doc))),
    ))
}

/// One A2A call, as the policy and the approval queue need it.
pub struct Call<'a> {
    pub tenant_id: &'a str,
    pub auth_ctx: &'a AuthContext,
    /// The agent card the call addresses: what an approval is asked for.
    pub card_id: &'a str,
    pub subject: Subject<'a>,
    /// The run the call is a step of; a hold names it.
    pub run: Option<RunScope>,
    /// The request's approvals: the tokens it carries and the questions
    /// its gates had answered (`agent_approval::RequestApprovals`).
    pub approvals: &'a agent_approval::RequestApprovals,
    /// The gates the request reaches after this route's (the skill a task
    /// is dispatched to), whose holds are raised with this one.
    pub downstream: Option<&'a dyn agent_approval::Downstream>,
    /// A person already approved this call to this card: another gate of the
    /// route asked for the approval and redeemed its one-time token. The
    /// question is the same ("may this caller call this card"), so a hold
    /// here is satisfied by it. (`approvals` knows the same thing; the route
    /// states it so that the order of the two gates is visible there.)
    pub approved: bool,
}

/// What the route does next.
#[derive(Debug)]
pub enum Outcome {
    Proceed,
    /// The policy refuses the call.
    Refused(String),
    /// The call is held for a person: the approval envelope and its status.
    Held(StatusCode, Value),
    /// The call needs an approval and the token it carries is not one.
    TokenRefused(String),
    /// The call needs an approval and the request for one could not be made.
    HoldFailed(String),
    /// As [`ActorOutcome::RefusedLater`].
    RefusedLater(agent_approval::DownstreamRefusal),
}

/// Judge one A2A call by its policy and, where the policy holds it for a
/// person, redeem the caller's approval or ask for one.
pub async fn enforce(state: &SharedState, policy: &Policy, call: Call<'_>) -> Outcome {
    if policy.is_empty() {
        return Outcome::Proceed;
    }
    let window_seconds = match policy.judge(&call.subject) {
        Verdict::Allow => return Outcome::Proceed,
        Verdict::Refuse(reason) => return Outcome::Refused(reason),
        Verdict::Hold { window_seconds } => window_seconds,
    };
    if call.approved {
        return Outcome::Proceed;
    }
    let ctx = agent_approval::ApprovalContext {
        db: &state.db,
        tenant_id: call.tenant_id,
        surface: "a2a",
        scope_ref: call.card_id,
        capability_type: ACTION,
        capability_name: call.card_id,
        auth_ctx: call.auth_ctx,
        groups: &[],
        policy_window_seconds: window_seconds,
        // This surface records no call arguments on the approval row.
        call_arguments: None,
        binding: None,
        run: call.run.clone(),
        extra_metadata: Some(serde_json::json!({
            "held_by": "a2a_policy",
            "policy": "capability.default_access=approval_required",
        })),
        run_clock: Some(&state.usage_state),
        approvals: call.approvals,
        downstream: call.downstream,
    };
    match agent_approval::require_or_enqueue(&ctx).await {
        agent_approval::ApprovalOutcome::Proceed => Outcome::Proceed,
        agent_approval::ApprovalOutcome::Envelope(status, body) => Outcome::Held(status, body),
        agent_approval::ApprovalOutcome::Denied(reason) => Outcome::TokenRefused(reason),
        agent_approval::ApprovalOutcome::Failed(reason) => Outcome::HoldFailed(reason),
        agent_approval::ApprovalOutcome::Refused(refusal) => Outcome::RefusedLater(refusal),
    }
}

// ---------------------------------------------------------------------------
// The acting principal's own plane
// ---------------------------------------------------------------------------

/// One A2A call, as the caller's own authorization needs it.
pub struct ActorCall<'a> {
    pub tenant_id: &'a str,
    pub auth_ctx: &'a AuthContext,
    /// The agent card the call addresses: the target of an `a2a_call` grant.
    pub card_id: &'a str,
    /// The group whose AI System's autonomy judges a caller that is not an
    /// agent.
    pub group_id: Option<&'a str>,
    /// The run the call is a step of; a hold names it.
    pub run: Option<RunScope>,
    /// The request's approvals: the tokens it carries and the questions
    /// its gates had answered (`agent_approval::RequestApprovals`).
    pub approvals: &'a agent_approval::RequestApprovals,
    /// The gates the request reaches after this route's (the skill a task
    /// is dispatched to), whose holds are raised with this one.
    pub downstream: Option<&'a dyn agent_approval::Downstream>,
}

/// What the route does next.
#[derive(Debug)]
pub enum ActorOutcome {
    /// The caller may make the call.
    Proceed {
        /// A person approved this call to this card: the agent's grant asked
        /// and the one-time token was redeemed. Hand it to [`Call::approved`].
        approved: bool,
        /// The agent is in dry-run mode and would have been refused or held:
        /// the decision to record as a shadow row. The call proceeds.
        shadow: Option<agent_authz::Outcome>,
    },
    /// The agent's grants refuse the call.
    Denied(agent_authz::Outcome),
    /// The agent's grant holds the call for a person.
    Held {
        status: StatusCode,
        envelope: Value,
        decision: agent_authz::Outcome,
    },
    /// The call needs an approval and the token it carries is not one. The
    /// text is the answer.
    TokenRefused(String),
    /// The call needs an approval and the request for one could not be made.
    HoldFailed {
        reason: String,
        decision: agent_authz::Outcome,
    },
    /// The agent's grants could not be read: the call is refused (fail
    /// closed), as an engine failure and not as a decision nobody made.
    Unavailable,
    /// The call has to be held or carries a token, and a gate it reaches
    /// after this route's (the skill a task is dispatched to) would refuse
    /// it: refused now with that gate's answer, nothing held, no token
    /// redeemed.
    RefusedLater(agent_approval::DownstreamRefusal),
    /// The caller is not an agent and its AI System's autonomy stops the
    /// call (suspended or restricted).
    AutonomyDenied {
        reason: String,
        resolution: autonomy::Resolution,
    },
}

/// May this caller call this card? The caller's own plane, for every A2A
/// route that does work, in one place.
///
/// * A caller that is an agent is judged by its grants for `a2a_call` on the
///   card (`agent_authz::decide_for_agent`, whose last step is the autonomy
///   gate of the agent's AI System). A grant that asks for an approval holds
///   the call through the approval queue, or redeems the token of an
///   approved one.
/// * Any other caller has no grants; its group's AI System's autonomy still
///   applies (`autonomy::enforce_for_group`): a suspended or restricted
///   system stops A2A work however the caller authenticated.
///
/// It runs before [`enforce`], which is told when a person approved the call
/// here (`ActorOutcome::Proceed::approved`), so the same question is not
/// asked twice. `tests/a2a_governance_guard.rs` refuses a route that does
/// work and does not come through here, and a copy of either gate in a route.
pub async fn authorize_actor(state: &SharedState, call: ActorCall<'_>) -> ActorOutcome {
    use agent_authz::Decision;
    let Some(agent_id) = call.auth_ctx.actor_agent_id.as_deref() else {
        return match autonomy::enforce_for_group(
            &state.db,
            call.tenant_id,
            call.group_id,
            ACTION,
            &[],
        )
        .await
        {
            Some((autonomy::Verdict::Deny(reason), resolution)) => ActorOutcome::AutonomyDenied {
                reason: reason.to_string(),
                resolution,
            },
            _ => ActorOutcome::Proceed {
                approved: false,
                shadow: None,
            },
        };
    };
    let decision = match agent_authz::decide_for_agent(
        &state.db,
        call.tenant_id,
        agent_id,
        ACTION,
        call.card_id,
        None,
        None,
        Some(&state.usage_state),
        &[],
    )
    .await
    {
        Ok(decision) => decision,
        Err(e) => {
            tracing::error!(error = %e, agent = %agent_id, card = %call.card_id, "a2a agent authz failed; failing closed");
            return ActorOutcome::Unavailable;
        }
    };
    if decision.decision == Decision::Allow {
        return ActorOutcome::Proceed {
            approved: false,
            shadow: None,
        };
    }
    if decision.dry_run {
        return ActorOutcome::Proceed {
            approved: false,
            shadow: Some(decision),
        };
    }
    if decision.decision == Decision::Deny {
        return ActorOutcome::Denied(decision);
    }
    // The grant asks for a person. One shared lifecycle
    // (`agent_approval::require_or_enqueue`): a valid one-time token is
    // consumed and the call proceeds; otherwise a request is enqueued. The
    // hold names its run, so the run ledger and the run clock count the wait.
    let ctx = agent_approval::ApprovalContext {
        db: &state.db,
        tenant_id: call.tenant_id,
        surface: "a2a",
        scope_ref: call.card_id,
        capability_type: ACTION,
        capability_name: call.card_id,
        auth_ctx: call.auth_ctx,
        groups: &[],
        // No policy on this plane states a window: the hold takes the group
        // / tenant / platform default (`approval_window`).
        policy_window_seconds: None,
        // This surface records no call arguments on the approval row; `None`
        // compares equal to the stored NULL.
        call_arguments: None,
        binding: None,
        run: call.run.clone(),
        extra_metadata: None,
        run_clock: Some(&state.usage_state),
        approvals: call.approvals,
        downstream: call.downstream,
    };
    match agent_approval::require_or_enqueue(&ctx).await {
        agent_approval::ApprovalOutcome::Proceed => ActorOutcome::Proceed {
            approved: true,
            shadow: None,
        },
        agent_approval::ApprovalOutcome::Envelope(status, envelope) => ActorOutcome::Held {
            status,
            envelope,
            decision,
        },
        agent_approval::ApprovalOutcome::Denied(reason) => {
            ActorOutcome::TokenRefused(format!("Approval token invalid: {reason}"))
        }
        agent_approval::ApprovalOutcome::Failed(reason) => {
            ActorOutcome::HoldFailed { reason, decision }
        }
        agent_approval::ApprovalOutcome::Refused(refusal) => ActorOutcome::RefusedLater(refusal),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn inbound<'a>(classifications: Vec<&'a Value>) -> Subject<'a> {
        Subject {
            direction: Direction::Inbound,
            classifications,
            work: true,
        }
    }

    fn outbound(url: &str) -> Subject<'_> {
        Subject {
            direction: Direction::Outbound { remote_url: url },
            classifications: Vec::new(),
            work: true,
        }
    }

    fn group(doc: Value) -> (Source, Value) {
        (Source::Group, doc)
    }

    fn refused(v: &Verdict) -> bool {
        matches!(v, Verdict::Refuse(_))
    }

    #[test]
    fn no_policy_allows() {
        assert!(Policy::default().is_empty());
        assert!(Policy::of([group(Value::Null)]).is_empty());
        assert_eq!(
            Policy::of([group(json!({}))]).judge(&inbound(vec![])),
            Verdict::Allow
        );
    }

    #[test]
    fn the_strictest_access_of_any_level_wins_in_either_order() {
        let enabled = json!({"capability": {"default_access": "enabled"}});
        let hold = json!({"capability": {"default_access": "approval_required"}});
        let disabled = json!({"capability": {"default_access": "disabled"}});
        for levels in [
            [enabled.clone(), hold.clone()],
            [hold.clone(), enabled.clone()],
        ] {
            assert_eq!(
                Policy::of(levels.map(group)).judge(&inbound(vec![])),
                Verdict::Hold {
                    window_seconds: None
                }
            );
        }
        for levels in [[hold.clone(), disabled.clone()], [disabled, hold]] {
            assert!(refused(
                &Policy::of(levels.map(group)).judge(&inbound(vec![]))
            ));
        }
    }

    #[test]
    fn the_shortest_stated_window_is_the_holds() {
        let p = Policy::of([
            group(json!({"capability": {"default_access": "approval_required",
                                        "approval_timeout_seconds": 300}})),
            group(json!({"capability": {"approval_timeout_seconds": 120}})),
            (
                Source::Card,
                json!({"capability": {"approval_timeout_seconds": 900}}),
            ),
        ]);
        assert_eq!(
            p.judge(&inbound(vec![])),
            Verdict::Hold {
                window_seconds: Some(120)
            }
        );
    }

    #[test]
    fn a_read_is_refused_by_disabled_and_not_held_by_approval_required() {
        let read = Subject {
            direction: Direction::Inbound,
            classifications: Vec::new(),
            work: false,
        };
        let hold = Policy::of([group(
            json!({"capability": {"default_access": "approval_required"}}),
        )]);
        assert_eq!(hold.judge(&read), Verdict::Allow);
        let disabled = Policy::of([group(json!({"capability": {"default_access": "disabled"}}))]);
        assert!(refused(&disabled.judge(&read)));
    }

    #[test]
    fn an_unknown_or_unreadable_access_refuses() {
        for doc in [
            json!({"capability": {"default_access": "audit_only"}}),
            json!({"capability": {"default_access": 1}}),
            json!({"capability": "disabled"}),
            json!("disabled"),
            json!([]),
        ] {
            assert!(
                refused(&Policy::of([group(doc.clone())]).judge(&inbound(vec![]))),
                "{doc}"
            );
        }
    }

    #[test]
    fn every_levels_denied_tags_apply_without_case() {
        let p = Policy::of([
            group(json!({"data": {"deny_classifications": ["pii"]}})),
            (
                Source::Card,
                json!({"data": {"deny_classifications": ["Secret"]}}),
            ),
        ]);
        let pii = json!(["PII"]);
        let secret = json!("SECRET");
        let public = json!(["public"]);
        assert_eq!(
            p.judge(&inbound(vec![&pii])),
            Verdict::Refuse("Group governance denies data classification `PII`".into())
        );
        assert_eq!(
            p.judge(&inbound(vec![&public, &secret])),
            Verdict::Refuse("Agent card policy denies data classification `SECRET`".into())
        );
        assert_eq!(p.judge(&inbound(vec![&public])), Verdict::Allow);
        assert_eq!(p.judge(&inbound(vec![])), Verdict::Allow);
    }

    #[test]
    fn a_classification_that_cannot_be_read_refuses_only_under_a_deny_list() {
        let odd = json!({"level": "PII"});
        let deny = Policy::of([group(json!({"data": {"deny_classifications": ["pii"]}}))]);
        assert!(refused(&deny.judge(&inbound(vec![&odd]))));
        let none = Policy::of([group(json!({"data": {"deny_classifications": []}}))]);
        assert_eq!(none.judge(&inbound(vec![&odd])), Verdict::Allow);
        let unreadable = Policy::of([group(json!({"data": {"deny_classifications": [1]}}))]);
        assert!(refused(&unreadable.judge(&inbound(vec![]))));
    }

    #[test]
    fn the_address_must_match_every_level_that_states_a_list() {
        let parent = json!({"remote": {"allowed_globs": ["https://*.partner.example/*"]}});
        let child = json!({"remote": {"allowed_globs": ["https://a.partner.example/*"]}});
        let p = Policy::of([group(parent.clone()), group(child)]);
        assert_eq!(
            p.judge(&outbound("https://a.partner.example/a2a")),
            Verdict::Allow
        );
        assert!(refused(
            &p.judge(&outbound("https://b.partner.example/a2a"))
        ));

        // Two lists with no pattern in common: nothing matches both. They
        // do not cancel out into "unrestricted".
        let other = json!({"remote": {"allowed_globs": ["https://other.example/*"]}});
        for levels in [[parent.clone(), other.clone()], [other, parent.clone()]] {
            let p = Policy::of(levels.map(group));
            assert!(refused(
                &p.judge(&outbound("https://a.partner.example/a2a"))
            ));
            assert!(refused(&p.judge(&outbound("https://other.example/a2a"))));
        }

        // A level with an empty list states nothing.
        let p = Policy::of([
            group(parent),
            group(json!({"remote": {"allowed_globs": []}})),
        ]);
        assert_eq!(
            p.judge(&outbound("https://a.partner.example/a2a")),
            Verdict::Allow
        );
    }

    #[test]
    fn a_list_with_no_readable_pattern_allows_nothing() {
        for globs in [json!(["http://[bad"]), json!([7]), json!("https://*")] {
            let p = Policy::of([group(json!({"remote": {"allowed_globs": globs}}))]);
            assert!(
                refused(&p.judge(&outbound("https://a.example/a2a"))),
                "{globs}"
            );
        }
    }

    #[test]
    fn an_allow_list_says_nothing_about_an_inbound_call() {
        let p = Policy::of([group(
            json!({"remote": {"allowed_globs": ["https://partner.example/*"]}}),
        )]);
        assert_eq!(p.judge(&inbound(vec![])), Verdict::Allow);
        // Not even an unreadable one: there is no address to compare.
        let p = Policy::of([group(json!({"remote": {"allowed_globs": ["http://[bad"]}}))]);
        assert_eq!(p.judge(&inbound(vec![])), Verdict::Allow);
    }

    #[test]
    fn the_refusal_names_where_the_list_is_set() {
        let p = Policy::of([(
            Source::Card,
            json!({"remote": {"allowed_globs": ["https://partner.example/*"]}}),
        )]);
        assert_eq!(
            p.judge(&outbound("https://x.example/a2a")),
            Verdict::Refuse(
                "Remote URL `https://x.example/a2a` is not in the agent card's allow-list".into()
            )
        );
    }
}
