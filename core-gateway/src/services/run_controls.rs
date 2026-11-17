// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The run-scoped controls, as ONE verdict every governed surface asks for.
//!
//! Two controls are scoped to a run rather than to a caller: the run budget
//! (`run_budget`) and an operator's abort
//! (`run_intervention`). Both answer "may *this run* take another step", and
//! neither cares how the caller authenticated.
//!
//! They used to be called separately at each surface, and each surface put
//! them beside its autonomy gate — which lives in the NON-agent branch,
//! because for an agent principal autonomy runs inside `decide_for_agent`.
//! So an agent-keyed call skipped the run caps on MCP, the LLM path and A2A
//! v1: the callers the run budget exists for were the ones it did not reach.
//! Nothing asked for that exemption; the sites were chosen by where the
//! autonomy call happened to be.
//!
//! [`check`] is the single place those two decisions are made. Each surface
//! calls it once, before its authorization planes (so a stopped run cannot
//! even queue an approval), and maps the [`RunRefusal`] onto its own wire
//! shape and audit row. The `run_controls_parity` integration suite drives
//! every surface × {agent, non-agent} × {aborted, over budget}.
//!
//! [`check`] is also where a step is counted against its run. Each caller
//! states what its step is ([`StepKind`]): a governed step that is not a
//! model call is admitted against the run's action ceiling
//! (`run_budget::TERM_MAX_ACTIONS_PER_RUN`) there, atomically on the shared
//! counters, and the caller holds the admission ([`StepAdmitted`]) until the
//! step is dispatched. A surface cannot ask the run controls without saying
//! whether its step counts; `tests/run_step_guard.rs` lists every answer.
//!
//! A run is also something a credential STARTS, and the group's envelope may
//! cap how many one credential starts in a window
//! (`run_budget::TERM_MAX_NEW_RUNS_PER_WINDOW`): without it a caller stays
//! under every per-run ceiling by putting each step in a run of its own. The
//! caller of [`check`] says who would be starting the step's run if it is
//! new ([`run_starter`]); whether it IS new is decided by the admission that
//! counts it, in the same atomic step as the action. A model call's is in
//! the call's own admission (`llm_preflight`).
//!
//! [`RunScope`] is the other half: the run a step belongs to, carried into
//! anything that outlives the request — chiefly an approval hold, which the
//! run ledger and the live run clock find by its `originating_trace_root_id`.

use axum::http::StatusCode;

use crate::services::a2a_delegation::CallChain;
use crate::services::autonomy::Verdict;
use crate::services::run_budget::RunCapBlock;

/// The run a governed step belongs to, and where in it.
///
/// Every surface derives one at request entry (from the verified call chain,
/// or a client-asserted run id), so every hold it raises can name its run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunScope {
    /// The run's `root_task_id`.
    pub run_id: String,
    /// This step's own task id — the parent of anything it spawns, including
    /// the decision row an approver's verdict writes.
    pub parent_task_id: Option<String>,
    /// `trace_root_kind` for a root step; `None` below the root.
    pub root_kind: Option<String>,
    /// Delegation hop, for the run's depth ceiling.
    pub depth: u32,
    /// The principal that starts this run if it is new ([`run_starter`]);
    /// `None` for a step that continues a run by a verified chain, and
    /// where the surface that received the request has counted it.
    pub starter: Option<String>,
}

impl RunScope {
    /// The scope of a step on `chain`, whose own task id is `current_task_id`.
    pub fn from_chain(chain: &CallChain, current_task_id: &str, root_kind: Option<&str>) -> Self {
        Self {
            run_id: chain.root_task_id.to_string(),
            parent_task_id: Some(current_task_id.to_string()),
            root_kind: root_kind.map(str::to_owned),
            depth: chain.depth,
            starter: None,
        }
    }

    /// The same scope, for a request whose run `starter` would be starting.
    pub fn started_by(mut self, starter: Option<String>) -> Self {
        self.starter = starter;
        self
    }

    /// The scope of a step known only by its run id (a client-asserted run
    /// on a surface that mints no step id of its own).
    pub fn for_run(run_id: impl Into<String>) -> Self {
        Self {
            run_id: run_id.into(),
            parent_task_id: None,
            root_kind: None,
            depth: 0,
            starter: None,
        }
    }
}

/// The principal a new run of this request is counted for: the agent
/// identity where the request has one, otherwise the API key.
///
/// `None` for a request that continues a run by a VERIFIED delegation chain
/// (`continues_verified_chain`): the gateway signed that chain for a run
/// whose first request was counted when it started it, and the step is not
/// the start of anything. `None` too for a caller that is neither (a signed-in
/// person): the limit is on credentials that drive runs unattended.
///
/// A run id the caller asserts, and a root the gateway mints (no run id, or a
/// chain it could not verify), are both runs this principal starts.
pub fn run_starter(
    auth: &crate::models::auth::AuthContext,
    continues_verified_chain: bool,
) -> Option<String> {
    if continues_verified_chain {
        return None;
    }
    auth.actor_agent_id
        .as_deref()
        .map(|agent| format!("agent:{agent}"))
        .or_else(|| auth.api_key_id.as_deref().map(|key| format!("key:{key}")))
}

/// Why a run may not take another step.
#[derive(Debug, Clone, PartialEq)]
pub enum RunRefusal {
    /// The run has reached one of its declared ceilings.
    CapReached(RunCapBlock),
    /// An operator stopped the run. Carries the operator-facing message.
    Aborted(&'static str),
    /// The step would start a run, and its credential has started as many
    /// as its group allows in the window. A rate refusal: the block every
    /// rate limit of the surface answers with.
    TooManyNewRuns(crate::services::usage::McpRateLimitBlock),
}

/// The `proxy_subtype` of a refused new run: `rate_limit_<scope>`, as every
/// rate refusal is recorded.
pub const NEW_RUN_REFUSED_SUBTYPE: &str = "rate_limit_credential";

impl RunRefusal {
    /// 429 for a ceiling (the run spent what it was allowed), 403 for an
    /// abort (a person said stop) — the codes every surface already used.
    pub fn status(&self) -> StatusCode {
        match self {
            Self::CapReached(_) | Self::TooManyNewRuns(_) => StatusCode::TOO_MANY_REQUESTS,
            Self::Aborted(_) => StatusCode::FORBIDDEN,
        }
    }

    /// The `proxy_subtype` the refusal's audit row carries (sealed `denied`).
    pub fn subtype(&self) -> &'static str {
        match self {
            Self::CapReached(_) => crate::constants::proxy_subtype::RUN_CAP_REACHED,
            Self::Aborted(_) => crate::constants::proxy_subtype::RUN_ABORTED,
            Self::TooManyNewRuns(_) => NEW_RUN_REFUSED_SUBTYPE,
        }
    }

    /// The rate-limit block of a refused new run, for a surface whose rate
    /// refusals have a shape of their own (JSON-RPC error, `Retry-After`).
    pub fn rate_block(&self) -> Option<&crate::services::usage::McpRateLimitBlock> {
        match self {
            Self::TooManyNewRuns(block) => Some(block),
            _ => None,
        }
    }

    pub fn message(&self) -> &str {
        match self {
            Self::CapReached(b) => &b.message,
            Self::Aborted(m) => m,
            Self::TooManyNewRuns(b) => &b.message,
        }
    }

    /// A short machine reason, for surfaces whose error envelope carries one.
    pub fn reason(&self) -> &'static str {
        match self {
            Self::CapReached(_) => "run_cap_reached",
            Self::Aborted(_) => "run_aborted",
            Self::TooManyNewRuns(_) => "too_many_new_runs",
        }
    }
}

/// What a step that asks the run controls is, for the run's counters.
///
/// Every caller of [`check`] states one. The run's action ceiling counts a
/// request the gateway governs that is not a model call, once, on the surface
/// that received it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepKind {
    /// A governed step that is not a model call, asked by the surface that
    /// received the request: an MCP capability call, a system server's tool,
    /// a skill execution, an agent-to-agent hop. Admitted against the run's
    /// action ceiling and, when its run is new, the limit on new runs.
    Action,
    /// A model call (chat, responses, messages, embeddings, media). Counted
    /// by the run's model-call, token and cost terms in the call's own
    /// admission, and never as an action. A new run it starts is counted in
    /// that admission too.
    ModelCall,
    /// Work done for a step the receiving surface has already admitted (a
    /// skill run by a system server's tool or for an agent-to-agent task).
    /// One request is one action.
    PartOfAction,
}

/// A step the run controls let through.
///
/// For a [`StepKind::Action`] of a run whose envelope caps its actions it
/// holds the unit the step was admitted against. The unit stays spent only
/// once the step is dispatched ([`Self::commit`], or the guard handed to the
/// surface's other holds with [`Self::into_hold`]); dropped before that (a
/// later gate refused or held the step) it goes back, as the surface's rate
/// and quota holds do.
#[must_use = "an admitted action is given back unless it is committed when the step is dispatched"]
#[derive(Default)]
pub struct StepAdmitted {
    action: Option<crate::services::cache::usage_state::HoldGuard>,
}

impl StepAdmitted {
    /// The step is dispatched: its action stays counted.
    pub async fn commit(self) {
        if let Some(hold) = self.action {
            hold.commit().await;
        }
    }

    /// The action's hold, for a surface that ends all of a request's holds
    /// together at dispatch.
    pub fn into_hold(self) -> Option<crate::services::cache::usage_state::HoldGuard> {
        self.action
    }
}

/// One step asking the run controls.
pub struct Step<'a> {
    pub tenant_id: &'a str,
    /// The governing resource group: its envelope holds the run's ceilings
    /// and the limit on new runs.
    pub group_id: Option<&'a str>,
    pub run_id: &'a str,
    /// Delegation hop, for the run's depth ceiling.
    pub depth: u32,
    pub kind: StepKind,
    /// The principal that starts the step's run if it is new
    /// ([`run_starter`]).
    pub starter: Option<&'a str>,
}

/// May this run take another step?
///
/// Budget first — a run over its ceiling is refused as such even if it was
/// also aborted, matching the order the surfaces used. The budget needs the
/// governing resource group (its envelope holds the caps); with no group it
/// is not gated, the module's fail-open. Abort needs only the run. A refused
/// abort is recorded on the intervention ("it actually bit").
///
/// Last, a [`StepKind::Action`] is admitted: against the limit on new runs
/// of its credential, when the step's run is one it would be starting, and
/// against the run's action ceiling. ONE atomic admission on the shared
/// counters holds both, so a step one of them refuses is counted by neither.
/// After the read-only verdicts, so a run that is aborted or over another
/// ceiling takes no unit. The counters or the group's caps being unreadable
/// refuses (fail closed).
pub async fn check(
    state: &crate::services::SharedState,
    step: Step<'_>,
) -> Result<StepAdmitted, RunRefusal> {
    let Step {
        tenant_id,
        group_id,
        run_id,
        depth,
        ..
    } = step;
    if let Some(gid) = group_id {
        if let Some(block) =
            crate::services::run_budget::check_run_caps(state, tenant_id, gid, run_id, depth).await
        {
            tracing::warn!(
                tenant_id = %tenant_id,
                group_id = %gid,
                run = %run_id,
                term = block.term,
                "run cap reached — refusing the next step",
            );
            return Err(RunRefusal::CapReached(block));
        }
    }

    if let Some(Verdict::Deny(reason)) = crate::services::run_intervention::check(
        &state.db,
        Some(&state.run_abort_cache),
        tenant_id,
        run_id,
    )
    .await
    {
        crate::services::run_intervention::record_block(&state.db, tenant_id, run_id).await;
        tracing::warn!(
            tenant_id = %tenant_id,
            run = %run_id,
            "run aborted by an operator — refusing the next step",
        );
        return Err(RunRefusal::Aborted(reason));
    }

    // A model call's counted terms (calls, tokens, cost) and the new run it
    // may start are admitted with the call's other limits in one atomic step
    // — `run_budget::run_dims`, `run_budget::new_runs_dim_for`. An action's
    // are admitted here.
    match (step.kind, group_id) {
        (StepKind::Action, Some(gid)) => admit_action(state, &step, gid).await,
        _ => Ok(StepAdmitted::default()),
    }
}

/// Admit one action of the step's run: the new run it may be, and the unit
/// of the run's action ceiling, as its group's envelope declares them.
async fn admit_action(
    state: &crate::services::SharedState,
    step: &Step<'_>,
    group_id: &str,
) -> Result<StepAdmitted, RunRefusal> {
    use crate::services::cache::usage_state::Shape;
    use crate::services::limits::{self, DimRefusal};
    use crate::services::run_budget::{self, TERM_RUN_USAGE_UNAVAILABLE};

    let (tenant_id, run_id) = (step.tenant_id, step.run_id);
    let unavailable = |what: &str| {
        RunRefusal::CapReached(RunCapBlock {
            term: TERM_RUN_USAGE_UNAVAILABLE,
            limit: 0.0,
            observed: 0.0,
            message: format!(
                "Refused: this run's {what} cannot be read, so its limits cannot be checked"
            ),
        })
    };
    // `check_run_caps` has just read (and cached) the same caps.
    let caps = match run_budget::caps_for_group(&state.db, tenant_id, group_id).await {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(tenant_id, group_id, error = %e, "run caps unreadable; refusing the action (fail closed)");
            return Err(unavailable("caps"));
        }
    };
    // The new run first: a step refused as one run too many is attributed
    // to that, whatever its run's own count.
    let mut dims = Vec::with_capacity(2);
    if let Some(starter) = step.starter {
        dims.extend(run_budget::new_runs_dim_for(
            &caps, tenant_id, starter, run_id,
        ));
    }
    let action = run_budget::action_dim_for(&caps, tenant_id, run_id);
    let held = action.is_some();
    dims.extend(action);
    if dims.is_empty() {
        return Ok(StepAdmitted::default());
    }
    // The action's unit is held until the step is dispatched. A new run is
    // counted for good, so an admission of that alone holds nothing.
    let hold_id = held.then(|| limits::HoldId::new(tenant_id, "run-action"));
    match limits::admit(&state.usage_state, hold_id.as_ref(), dims, Shape::count()).await {
        Ok(Ok(admitted)) => Ok(StepAdmitted {
            action: admitted.hold.map(|h| state.usage_state.adopt_hold(h)),
        }),
        Ok(Err(refused)) => match &refused.refusal {
            DimRefusal::Run { term, limit, .. } => {
                let block = run_budget::refusal_block(term, *limit, refused.current);
                tracing::warn!(
                    tenant_id = %tenant_id,
                    group_id = %group_id,
                    run = %run_id,
                    term = block.term,
                    "run action ceiling reached — refusing the next step",
                );
                Err(RunRefusal::CapReached(block))
            }
            DimRefusal::Rate { .. } => match refused.rate_block() {
                Some(block) => {
                    tracing::warn!(
                        tenant_id = %tenant_id,
                        group_id = %group_id,
                        run = %run_id,
                        starter = step.starter.unwrap_or(""),
                        "too many new runs for this credential — refusing the step",
                    );
                    Err(RunRefusal::TooManyNewRuns(block))
                }
                None => Err(unavailable("usage")),
            },
            // The admission holds no other kind of dimension.
            _ => Err(unavailable("usage")),
        },
        Err(e) => {
            tracing::error!(tenant_id, run_id, error = %e, "run counters unreachable; refusing (fail closed)");
            Err(unavailable("usage"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cap() -> RunRefusal {
        RunRefusal::CapReached(RunCapBlock {
            term: crate::services::run_budget::TERM_MAX_CALLS_PER_RUN,
            limit: 1.0,
            observed: 1.0,
            message: "ceiling".to_string(),
        })
    }

    #[test]
    fn refusals_map_to_the_codes_and_subtypes_surfaces_already_used() {
        assert_eq!(cap().status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(
            cap().subtype(),
            crate::constants::proxy_subtype::RUN_CAP_REACHED
        );
        assert_eq!(cap().message(), "ceiling");
        let aborted = RunRefusal::Aborted(crate::services::run_intervention::ABORTED_MESSAGE);
        assert_eq!(aborted.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            aborted.subtype(),
            crate::constants::proxy_subtype::RUN_ABORTED
        );
        assert_eq!(aborted.reason(), "run_aborted");
    }

    /// A refused new run is a rate refusal: 429, recorded as
    /// `rate_limit_<scope>` like every rate limit, with its block for the
    /// surfaces whose rate refusals have a shape of their own.
    #[test]
    fn a_refused_new_run_is_a_rate_refusal() {
        use crate::services::cache::usage_state::{Shape, UsageState};
        use crate::services::limits;
        let us = UsageState::new(None);
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let admit = |run: &str| {
            let dim = limits::new_runs_dim("t", "key:k1", run, 1, 3600, "max_new_runs_per_window");
            rt.block_on(limits::admit(&us, None, vec![dim], Shape::count()))
                .unwrap()
        };
        assert!(admit("run-1").is_ok());
        assert!(admit("run-1").is_ok(), "a run already started continues");
        let refused = admit("run-2").expect_err("one run too many");
        let block = refused.rate_block().expect("a rate refusal");
        assert_eq!(
            (block.scope, block.limit_type),
            (limits::NEW_RUNS_SCOPE, limits::NEW_RUNS_LIMIT_TYPE)
        );
        assert!(
            block.message.contains("max_new_runs_per_window"),
            "{}",
            block.message
        );
        assert!(block.message.contains("(1/1)"), "{}", block.message);
        assert!((1..=3600).contains(&block.retry_after_secs));

        let refusal = RunRefusal::TooManyNewRuns(block.clone());
        assert_eq!(refusal.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(refusal.subtype(), NEW_RUN_REFUSED_SUBTYPE);
        assert_eq!(
            refusal.subtype(),
            format!(
                "{}{}",
                crate::services::llm_refusal::RATE_LIMIT_PREFIX,
                limits::NEW_RUNS_SCOPE
            )
        );
        assert_eq!(refusal.message(), block.message);
        assert_eq!(refusal.rate_block(), Some(&block));
        assert_eq!(cap().rate_block(), None);
    }

    /// The principal a new run is counted for: the agent identity where the
    /// request has one, otherwise the API key; nobody for a step on a
    /// verified chain, and nobody for a caller that is neither.
    #[test]
    fn a_new_run_is_counted_for_the_agent_else_the_key() {
        use crate::models::auth::{AuthContext, AuthType};
        let mut auth: AuthContext = crate::models::auth::make_auth_context(AuthType::ApiKey, "t");
        assert_eq!(
            run_starter(&auth, false),
            None,
            "neither an agent nor a key"
        );
        auth.api_key_id = Some("k1".into());
        assert_eq!(run_starter(&auth, false).as_deref(), Some("key:k1"));
        auth.actor_agent_id = Some("a1".into());
        assert_eq!(
            run_starter(&auth, false).as_deref(),
            Some("agent:a1"),
            "an agent's runs are the agent's, whichever key it used"
        );
        assert_eq!(
            run_starter(&auth, true),
            None,
            "a verified chain starts no run"
        );
        // An agent that signed in without a key.
        auth.api_key_id = None;
        assert_eq!(run_starter(&auth, false).as_deref(), Some("agent:a1"));
    }

    #[test]
    fn a_scope_from_a_chain_names_its_run_step_and_depth() {
        let root = ulid::Ulid::new();
        let chain = CallChain::root(root);
        let s = RunScope::from_chain(&chain, "step-1", Some("agent_invocation"));
        assert_eq!(s.run_id, root.to_string());
        assert_eq!(s.parent_task_id.as_deref(), Some("step-1"));
        assert_eq!(s.root_kind.as_deref(), Some("agent_invocation"));
        assert_eq!(s.depth, 0);
        assert_eq!(s.starter, None);
        let s = s.started_by(Some("key:k1".into()));
        assert_eq!(s.starter.as_deref(), Some("key:k1"));
    }
}
