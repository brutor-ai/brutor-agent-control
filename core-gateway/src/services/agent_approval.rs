// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Surface-generic human-in-the-loop approval queue for governed *actions*.
//!
//! Extracted from the MCP tool-approval path so the identical
//! enqueue/validate/consume lifecycle can back per-agent `approval_required`
//! grants on **any** action surface — MCP tools, A2A calls, and skill
//! executions. (Approval is for actions, not raw LLM inference; the LLM
//! surface fails an `approval_required` outcome safe to 403.)
//!
//! The single storage table `tool_approval_requests` is reused verbatim. Two
//! columns are general so non-MCP surfaces fit: the
//! historically MCP-only `server_config_id` is now nullable, and
//! `requester_type` accepts `'agent'`. The `server_name`/`capability_type`/
//! `capability_name` triplet is treated as a generic
//! `(scope_ref, action_type, action_name)`:
//!
//! | surface | scope_ref        | capability_type | capability_name |
//! |---------|------------------|-----------------|-----------------|
//! | mcp     | mcp server name  | tool/resource…  | tool name       |
//! | a2a     | agent card id    | a2a_call        | capability      |
//! | skill   | skill id         | skill_exec      | skill name      |
//!
//! `request_metadata` records `{agent_id, surface}` so the resolver in
//! `routes/approvals.rs` can write a surface-correct decision audit row.
//!
//! A request can pass several gates that ask for a person. They share one
//! [`RequestApprovals`], which states the rule: one approval per question,
//! every hold of the request raised in one answer, every token carried on
//! one retry.

use serde_json::{Value, json};

use axum::http::StatusCode;

use crate::models::auth::AuthContext;
use crate::models::db::ResourceGroup;
use crate::services::db_errors::DbResultExt;

/// Everything `require_or_enqueue` needs to decide one gate of a request.
pub struct ApprovalContext<'a> {
    pub db: &'a sqlx::PgPool,
    pub tenant_id: &'a str,
    /// "mcp" | "a2a" | "skill" — see the surface table above.
    pub surface: &'a str,
    pub scope_ref: &'a str,
    pub capability_type: &'a str,
    pub capability_name: &'a str,
    pub auth_ctx: &'a AuthContext,
    pub groups: &'a [ResourceGroup],
    /// The window the POLICY that demanded a human states for this action,
    /// if it states one (a capability-filter row, the group-wide MCP
    /// default). `None` = the policy names no window, and the hold takes
    /// the Resource Group / tenant / platform default. Never a resolved
    /// number: [`require_or_enqueue`] resolves the window itself through
    /// `approval_window::resolve`, the one resolver every hold uses
    /// (guarded by `tests/approval_hold_attribution_guard.rs`).
    pub policy_window_seconds: Option<i32>,
    /// The arguments this call carries. Bound into the issued token,
    /// so approving one set cannot authorise a different one on retry.
    pub call_arguments: Option<&'a Value>,
    /// What the approval is bound to when that is not `call_arguments`
    /// ([`content_binding`]). `None`: the arguments are the binding.
    pub binding: Option<&'a str>,
    /// The run this action belongs to. Persisted as the hold's
    /// `originating_trace_root_id` / `_parent_task_id` / `_trace_root_kind`:
    /// the run ledger and the live run clock find a run's holds by that
    /// root, so a hold raised without it is time the run spent waiting on a
    /// person that neither of them can see. `None` only when the request
    /// genuinely belongs to no run yet (an A2A v1 call with no asserted run,
    /// whose task — its eventual run — has not been created). Every surface
    /// passes the value it derived from the request; `tests/
    /// approval_hold_attribution_guard.rs` refuses a literal `None` here.
    pub run: Option<crate::services::run_controls::RunScope>,
    /// Merged into `request_metadata` alongside `{agent_id, surface}`.
    /// It tells the approver which policy tripped and
    /// on what number, so the queue can be acted on rather than
    /// rubber-stamped.
    pub extra_metadata: Option<Value>,
    /// The run clock behind the per-run duration ceiling. When present, a
    /// raised hold keeps the run's clock alive for the hold window, and a
    /// redeemed one pauses the clock for the time the run spent waiting on a
    /// person (see `run_budget::sync_run_pause_with`). The gate reconciles
    /// the pause from the database on its next check either way.
    pub run_clock: Option<&'a crate::services::cache::usage_state::UsageState>,
    /// The approval tokens the request carries and the questions it has
    /// already had answered: one object per request, shared by every gate of
    /// the request (see [`RequestApprovals`]).
    pub approvals: &'a RequestApprovals,
    /// The gates this request reaches AFTER this one that may ask a
    /// different question (a skill behind an A2A task or behind
    /// `skills__run_script`). `None`: this gate is the request's last.
    pub downstream: Option<&'a dyn Downstream>,
}

impl ApprovalContext<'_> {
    /// The run this action belongs to, if any.
    pub fn run_id(&self) -> Option<&str> {
        self.run.as_ref().map(|r| r.run_id.as_str())
    }
}

/// What the caller should do next.
pub enum ApprovalOutcome {
    /// A valid token was presented and consumed — carry on with the action.
    Proceed,
    /// No token: an approval request was enqueued. Return this envelope
    /// verbatim; the proxy-log row must preview these exact bytes.
    Envelope(StatusCode, Value),
    /// A token was presented and rejected. Operator-facing reason.
    Denied(String),
    /// The enqueue itself failed. Distinct from `Denied` because it is a
    /// 500, not a 403 — the caller did nothing wrong.
    Failed(String),
    /// A gate the request reaches after this one would refuse it
    /// ([`Downstream`]). Nothing was held and no token was redeemed: the
    /// answer is that gate's refusal, now.
    Refused(DownstreamRefusal),
}

/// The refusal of a later gate of the request, as that gate would give it.
#[derive(Debug, Clone)]
pub struct DownstreamRefusal {
    pub status: StatusCode,
    pub message: String,
    /// The gate's own JSON body, when it has one.
    pub body: Option<Value>,
    /// The refusal subtype the audit row carries.
    pub subtype: Option<&'static str>,
    /// An argument policy denied the call (`proxy_logger::apply_policy_deny`).
    pub argument_policy_denied: bool,
}

/// What an approval is asked for: the exact content of one action. Two gates
/// of one request that state the same question are asking a person the same
/// thing, and one answer serves both; a gate that states another question
/// needs an answer of its own. (The requester is part of the question too:
/// it is the same for every gate of one request and is compared when a token
/// is examined.)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Question {
    scope_ref: String,
    capability_type: String,
    capability_name: String,
    /// Canonical form of the arguments ([`canonical_arguments`]).
    arguments: String,
}

impl Question {
    pub fn of(
        scope_ref: &str,
        capability_type: &str,
        capability_name: &str,
        call_arguments: Option<&Value>,
    ) -> Self {
        Self {
            scope_ref: scope_ref.to_string(),
            capability_type: capability_type.to_string(),
            capability_name: capability_name.to_string(),
            arguments: canonical_arguments(call_arguments),
        }
    }

    /// The question of a call whose approval is bound to `binding`
    /// ([`content_binding`]) rather than to the arguments a person is shown:
    /// the content of the call before a guardrail redacted it. `None` binds
    /// the arguments themselves ([`Question::of`]).
    pub fn bound_to(
        scope_ref: &str,
        capability_type: &str,
        capability_name: &str,
        call_arguments: Option<&Value>,
        binding: Option<&str>,
    ) -> Self {
        match binding {
            None => Self::of(scope_ref, capability_type, capability_name, call_arguments),
            Some(binding) => Self {
                scope_ref: scope_ref.to_string(),
                capability_type: capability_type.to_string(),
                capability_name: capability_name.to_string(),
                arguments: format!("{BOUND_PREFIX}{binding}"),
            },
        }
    }

    /// How a token issued for `self` fails to answer `asked`.
    fn mismatch(&self, asked: &Question) -> Mismatch {
        if self.scope_ref != asked.scope_ref {
            Mismatch::Scope
        } else if self.capability_type != asked.capability_type
            || self.capability_name != asked.capability_name
        {
            Mismatch::Capability
        } else {
            Mismatch::Arguments
        }
    }
}

/// Marks [`Question::arguments`] as a content binding. Canonical JSON never
/// starts with it, so a binding equals no argument object.
const BOUND_PREFIX: &str = "bound:";

/// The `request_metadata` key an approval's content binding is stored under.
const BOUND_TO: &str = "bound_to";

/// What an approval is bound to when the arguments it shows are not what the
/// caller sent: a keyed digest (HMAC-SHA256 under `secret`, hex) of the
/// canonical form of `content`, the call as the caller sent it.
///
/// A surface whose input guardrail redacts the call before its handler runs
/// shows the approver the redacted call and gives the handler the redacted
/// call, and the redaction can differ from one evaluation to the next. The
/// approval is for the caller's call, so that is what the token is compared
/// on. The digest is keyed: the approval row holds no value from which what
/// the guardrail removed could be guessed and confirmed.
pub fn content_binding(secret: &str, content: Option<&Value>) -> String {
    use hmac::Mac;
    let mut mac = hmac::Hmac::<sha2::Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 accepts keys of any length");
    mac.update(b"approval-content-binding\0");
    mac.update(canonical_arguments(content).as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// How an approval differs from a question it does not answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mismatch {
    Scope,
    Capability,
    Arguments,
}

impl Mismatch {
    /// How much of the question the approval does share.
    fn closeness(self) -> u8 {
        match self {
            Mismatch::Scope => 0,
            Mismatch::Capability => 1,
            Mismatch::Arguments => 2,
        }
    }

    fn reason(self) -> &'static str {
        match self {
            Mismatch::Scope => "Token scope mismatch",
            Mismatch::Capability => "Token capability mismatch",
            Mismatch::Arguments => {
                "Token argument mismatch: the approved call and the retried call differ"
            }
        }
    }
}

/// At most this many approval tokens are read from one request. No request
/// has that many gates; a longer list is refused rather than looked up.
pub const MAX_APPROVAL_TOKENS: usize = 8;

/// The approvals of ONE request: the one-time tokens it carries and the
/// questions it has had answered so far.
///
/// One rule for every surface:
///
/// * An approval is for the request's exact content. Every gate of the
///   request that asks the same question (same requester, same action, same
///   canonical arguments) is satisfied by it: the first such gate redeems the
///   token, the others find the question answered.
/// * A gate that asks a different question needs an approval of its own. A
///   request carries one token per question, each in its own
///   `X-Approval-Token` header (or comma-separated in one); each gate takes
///   the token that matches its question.
/// * When a gate has to hold the request, the holds of the request's later
///   gates ([`Downstream`]) are raised in the same answer, so the approver
///   sees them together and the caller retries once, carrying every token.
///   A token is redeemed only when no gate of the request still lacks one.
/// * A token that matches no gate of the request refuses the request; no
///   token is redeemed.
///
/// Tokens stay one-time and bound to the exact call
/// ([`require_or_enqueue`]).
#[derive(Debug, Default)]
pub struct RequestApprovals {
    tokens: Vec<String>,
    /// The request's token header could not be read; a gate that needs a
    /// token answers with this.
    unreadable: Option<&'static str>,
    state: std::sync::Mutex<Redeemed>,
}

#[derive(Debug, Default)]
struct Redeemed {
    answered: Vec<Question>,
    spent: Vec<String>,
}

impl RequestApprovals {
    /// A request that carries no token.
    pub fn none() -> Self {
        Self::default()
    }

    /// Every `X-Approval-Token` of the request: each header value, split at
    /// commas.
    pub fn from_headers(headers: &axum::http::HeaderMap) -> Self {
        let mut out = Self::default();
        for value in headers.get_all(crate::constants::headers::X_APPROVAL_TOKEN) {
            match value.to_str() {
                Ok(v) => out.add(v),
                Err(_) => out.unreadable = Some("Invalid X-Approval-Token header encoding"),
            }
        }
        out
    }

    /// The same with tokens that arrived outside the headers (a request
    /// body, a tool argument); `None` adds nothing.
    pub fn with(mut self, tokens: Option<&str>) -> Self {
        if let Some(tokens) = tokens {
            self.add(tokens);
        }
        self
    }

    fn add(&mut self, raw: &str) {
        for token in raw.split(',').map(str::trim).filter(|t| !t.is_empty()) {
            if self.tokens.iter().any(|t| t == token) {
                continue;
            }
            if self.tokens.len() == MAX_APPROVAL_TOKENS {
                self.unreadable = Some("Too many approval tokens on one request");
                return;
            }
            self.tokens.push(token.to_string());
        }
    }

    /// Whether the request carries no token at all.
    pub fn is_empty(&self) -> bool {
        self.tokens.is_empty() && self.unreadable.is_none()
    }

    fn state(&self) -> std::sync::MutexGuard<'_, Redeemed> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Whether a gate of this request already had `question` answered.
    pub fn answered(&self, question: &Question) -> bool {
        self.state().answered.contains(question)
    }

    /// The tokens no gate of this request has redeemed yet.
    fn unspent(&self) -> Vec<String> {
        let state = self.state();
        self.tokens
            .iter()
            .filter(|t| !state.spent.contains(t))
            .cloned()
            .collect()
    }

    fn redeemed(&self, token: &str, question: Question) {
        let mut state = self.state();
        state.spent.push(token.to_string());
        state.answered.push(question);
    }
}

/// A hold a later gate of the same request would raise: everything
/// [`require_or_enqueue`] needs to raise it now, beside the hold of the gate
/// that is asking (the requester, the tenant and the run are the request's).
#[derive(Debug, Clone)]
pub struct DownstreamHold {
    pub surface: &'static str,
    pub scope_ref: String,
    pub capability_type: String,
    pub capability_name: String,
    pub groups: Vec<ResourceGroup>,
    pub policy_window_seconds: Option<i32>,
    pub call_arguments: Option<Value>,
    /// As [`ApprovalContext::binding`].
    pub binding: Option<String>,
    pub extra_metadata: Option<Value>,
}

impl DownstreamHold {
    fn question(&self) -> Question {
        Question::bound_to(
            &self.scope_ref,
            &self.capability_type,
            &self.capability_name,
            self.call_arguments.as_ref(),
            self.binding.as_deref(),
        )
    }
}

/// The gates a request reaches after the one that is asking. Asked only when
/// a gate examines a token or has to hold, never on a call no gate stops.
pub trait Downstream: Sync {
    /// The holds those gates would raise for this request as it stands, or
    /// the refusal one of them would give it. A request a later gate
    /// refuses is refused here, before any hold is raised and before any
    /// token is redeemed: a hold for a call that cannot run is a question
    /// nobody can usefully answer.
    fn holds(
        &self,
    ) -> futures::future::BoxFuture<'_, Result<Vec<DownstreamHold>, DownstreamRefusal>>;
}

/// One hold to raise: the asking gate's own, or a downstream gate's.
struct HoldSpec<'h> {
    surface: &'h str,
    scope_ref: &'h str,
    capability_type: &'h str,
    capability_name: &'h str,
    groups: &'h [ResourceGroup],
    policy_window_seconds: Option<i32>,
    call_arguments: Option<&'h Value>,
    binding: Option<&'h str>,
    extra_metadata: Option<&'h Value>,
}

/// Redeem the request's approval for this gate's question, or hold the
/// request for a person.
///
/// This is the whole `approval_required` lifecycle for one gate, in one
/// place, on every surface (MCP tools, A2A calls, skill executions). The
/// rule is [`RequestApprovals`]'s:
///
/// 1. The question was answered earlier in this request: proceed.
/// 2. Every token the request still carries is examined before any is
///    redeemed: it must exist, be approved, belong to this requester, be
///    inside its validity, and answer this gate's question or a downstream
///    gate's. Otherwise the request is refused and nothing is redeemed.
/// 3. This gate and every downstream gate has its token: this gate's token
///    is claimed atomically (one-time) and the request proceeds; each
///    downstream gate claims its own when the request reaches it.
/// 4. Otherwise a hold is raised for every gate that lacks a token, all in
///    one envelope. Tokens presented for the other gates are not redeemed:
///    the caller sends them again with the new ones.
pub async fn require_or_enqueue(ctx: &ApprovalContext<'_>) -> ApprovalOutcome {
    let own = Question::bound_to(
        ctx.scope_ref,
        ctx.capability_type,
        ctx.capability_name,
        ctx.call_arguments,
        ctx.binding,
    );
    if ctx.approvals.answered(&own) {
        return ApprovalOutcome::Proceed;
    }
    if let Some(reason) = ctx.approvals.unreadable {
        return ApprovalOutcome::Denied(reason.to_string());
    }

    // The request's later gates that ask something else.
    let mut downstream: Vec<DownstreamHold> = Vec::new();
    if let Some(later) = ctx.downstream {
        let holds = match later.holds().await {
            Ok(holds) => holds,
            Err(refusal) => return ApprovalOutcome::Refused(refusal),
        };
        for hold in holds {
            let question = hold.question();
            if question != own
                && !ctx.approvals.answered(&question)
                && !downstream.iter().any(|d| d.question() == question)
            {
                downstream.push(hold);
            }
        }
    }

    let asked_later: Vec<Question> = downstream.iter().map(DownstreamHold::question).collect();

    // Examine every token before redeeming any.
    let mut own_token: Option<(String, String)> = None;
    let mut covered: Vec<Question> = Vec::new();
    for token in ctx.approvals.unspent() {
        let approval = match examine_token(ctx.db, &token, ctx.tenant_id, ctx.auth_ctx).await {
            Ok(approval) => approval,
            Err(reason) => return ApprovalOutcome::Denied(reason),
        };
        if approval.question == own {
            own_token.get_or_insert((token, approval.request_id));
        } else if downstream.iter().any(|d| d.question() == approval.question) {
            covered.push(approval.question);
        } else {
            // Say how the token differs from the question it comes
            // closest to: this gate's, or a later gate's.
            let mut reason = std::iter::once(&own)
                .chain(asked_later.iter())
                .map(|asked| approval.question.mismatch(asked))
                .max_by_key(|m| m.closeness())
                .unwrap_or(Mismatch::Scope)
                .reason()
                .to_string();
            if !downstream.is_empty() {
                reason.push_str(" (the token answers no approval this request needs)");
            }
            return ApprovalOutcome::Denied(reason);
        }
    }
    let lacking: Vec<&DownstreamHold> = downstream
        .iter()
        .filter(|d| !covered.contains(&d.question()))
        .collect();

    if let (Some((token, request_id)), true) = (&own_token, lacking.is_empty()) {
        if let Err(reason) = claim_token(ctx.db, request_id).await {
            return ApprovalOutcome::Denied(reason);
        }
        tracing::debug!(
            request_id,
            "Approval token consumed for {} '{}'",
            ctx.capability_type,
            ctx.capability_name
        );
        ctx.approvals.redeemed(token, own);
        // Record into the request-scoped task-local so the retried call's
        // audit row names the approval that authorised it.
        crate::middleware::run_end::record_consumed_approval(request_id);
        // The hold is over: take the time the run sat waiting on a person
        // off its clock, so the approved retry is not refused by the
        // duration ceiling for having been approved slowly.
        if let (Some(clock), Some(run_id)) = (ctx.run_clock, ctx.run_id()) {
            crate::services::run_budget::sync_run_pause_with(
                ctx.db,
                clock,
                ctx.tenant_id,
                run_id,
                None,
                None,
            )
            .await;
        }
        return ApprovalOutcome::Proceed;
    }

    // Hold the request: one approval request per gate that lacks a token.
    let mut specs: Vec<HoldSpec<'_>> = Vec::new();
    if own_token.is_none() {
        specs.push(HoldSpec {
            surface: ctx.surface,
            scope_ref: ctx.scope_ref,
            capability_type: ctx.capability_type,
            capability_name: ctx.capability_name,
            groups: ctx.groups,
            policy_window_seconds: ctx.policy_window_seconds,
            call_arguments: ctx.call_arguments,
            binding: ctx.binding,
            extra_metadata: ctx.extra_metadata.as_ref(),
        });
    }
    specs.extend(lacking.iter().map(|d| HoldSpec {
        surface: d.surface,
        scope_ref: &d.scope_ref,
        capability_type: &d.capability_type,
        capability_name: &d.capability_name,
        groups: &d.groups,
        policy_window_seconds: d.policy_window_seconds,
        call_arguments: d.call_arguments.as_ref(),
        binding: d.binding.as_deref(),
        extra_metadata: d.extra_metadata.as_ref(),
    }));
    let kept = usize::from(own_token.is_some()) + covered.len();
    let mut raised: Vec<(StatusCode, Value, i32)> = Vec::new();
    for spec in &specs {
        match raise_hold(ctx, spec).await {
            Ok(hold) => raised.push(hold),
            Err(e) => return ApprovalOutcome::Failed(e),
        }
    }
    // A hold may last up to its timeout. Keep the run's clock (and its
    // counters) alive for that long plus the normal run TTL: a `t0` that
    // lapsed mid-hold would be re-created on the retry and hand the run a
    // fresh clock — escaping the ceiling by waiting.
    if let (Some(clock), Some(run_id)) = (ctx.run_clock, ctx.run_id()) {
        let longest = raised.iter().map(|(_, _, secs)| *secs).max().unwrap_or(0);
        let hold_secs = u64::try_from(longest.max(0)).unwrap_or(0);
        clock
            .run_keep_alive(
                ctx.tenant_id,
                run_id,
                hold_secs + crate::services::cache::usage_state::RUN_TTL_SECS,
            )
            .await;
    }
    let (status, body) = held_answer(raised, kept);
    ApprovalOutcome::Envelope(status, body)
}

/// The answer to a held request. One hold and no token kept: that hold's
/// envelope, as it always was. Otherwise the first hold's envelope with
/// every hold listed under `approvals` and a message that says how to retry.
fn held_answer(mut raised: Vec<(StatusCode, Value, i32)>, kept: usize) -> (StatusCode, Value) {
    let bodies: Vec<Value> = raised.iter().map(|(_, body, _)| body.clone()).collect();
    let (status, mut body, _) = raised.swap_remove(0);
    if bodies.len() == 1 && kept == 0 {
        return (status, body);
    }
    let needed = bodies.len() + kept;
    let mut message = format!(
        "This call needs {needed} approvals. Retry it once with every approval token, each in \
         its own X-Approval-Token header."
    );
    if kept > 0 {
        message.push_str(&format!(
            " The {kept} token(s) sent with this call were not used and stay valid: send them \
             again."
        ));
    }
    if let Some(obj) = body.as_object_mut() {
        obj.insert("approvals".to_string(), Value::Array(bodies));
        obj.insert("approvals_needed".to_string(), json!(needed));
        obj.insert("message".to_string(), json!(message));
    }
    (status, body)
}

/// Raise one hold: resolve its window through the one resolver (policy >
/// group chain min > tenant > platform) for the group the hold is routed
/// to, and write its row. Returns the envelope and the window.
async fn raise_hold(
    ctx: &ApprovalContext<'_>,
    spec: &HoldSpec<'_>,
) -> Result<(StatusCode, Value, i32), String> {
    let hold_group = crate::services::approval_window::hold_group_id(ctx.auth_ctx, spec.groups);
    let window = crate::services::approval_window::resolve(
        ctx.db,
        ctx.tenant_id,
        hold_group.as_deref(),
        spec.policy_window_seconds,
    )
    .await;
    // Say where the window came from, beside the policy detail.
    let mut extra_metadata = spec.extra_metadata.cloned().unwrap_or_else(|| json!({}));
    if let Some(obj) = extra_metadata.as_object_mut() {
        obj.insert(
            "approval_window_source".to_string(),
            json!(window.source.as_str()),
        );
        // A policy's own detail never states the binding.
        obj.remove(BOUND_TO);
        if let Some(binding) = spec.binding {
            obj.insert(BOUND_TO.to_string(), json!(binding));
        }
    }
    let (status, body) = create_approval_request_with_metadata(
        ctx.db,
        ctx.tenant_id,
        spec.surface,
        spec.scope_ref,
        spec.capability_type,
        spec.capability_name,
        ctx.auth_ctx,
        hold_group.unwrap_or_default(),
        window.seconds,
        spec.call_arguments,
        ctx.run.as_ref().map(|r| r.run_id.clone()),
        ctx.run.as_ref().and_then(|r| r.parent_task_id.clone()),
        ctx.run.as_ref().and_then(|r| r.root_kind.clone()),
        Some(extra_metadata),
    )
    .await?;
    Ok((status, body, window.seconds))
}

/// How long an approved-but-unused token stays usable.
///
/// Approval previously had no expiry at all on the approved
/// side: the sweeper only expires rows still in `pending`, so a token a human
/// granted and nobody spent stayed valid for the life of the row. A human who
/// approves "yes, refund this order" is answering a question asked *now*;
/// treating that answer as good a week later is not what they agreed to.
const APPROVED_TOKEN_TTL_SECS: i64 = 3600;

/// Who raised a hold, as its row records it. Only the same principal may
/// redeem the approval: the token used to be a bearer secret, so any
/// caller in the tenant who obtained it (a log line, a shared channel, an
/// approver's screen) could execute the approved action as their own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Requester {
    /// `agent` | `api_key` | `portal_user` (the `requester_type` column).
    pub kind: String,
    pub user_id: Option<String>,
    pub api_key_id: Option<String>,
    /// `request_metadata.agent_id`.
    pub agent_id: Option<String>,
    /// `request_metadata.requester_idp_user_id`: the IdP subject of a caller
    /// with no gateway user (an MCP client signed in through OAuth).
    pub idp_user_id: Option<String>,
}

impl Requester {
    /// The principal behind `ctx`, as a hold it raises records it.
    pub fn of(ctx: &AuthContext) -> Self {
        let kind = if ctx.actor_agent_id.is_some() {
            "agent"
        } else if ctx.api_key_id.is_some() {
            "api_key"
        } else {
            "portal_user"
        };
        Self {
            kind: kind.to_string(),
            user_id: ctx.gateway_user_id.clone(),
            api_key_id: ctx.api_key_id.clone(),
            agent_id: ctx.actor_agent_id.clone(),
            idp_user_id: ctx.idp_user_id.clone(),
        }
    }

    /// Whether `ctx` is the principal that raised the hold. Each kind is
    /// matched on the identity that defines it; a recorded identity that is
    /// missing matches nothing.
    pub fn is(&self, ctx: &AuthContext) -> bool {
        let same = |recorded: &Option<String>, presented: &Option<String>| {
            recorded.is_some() && recorded == presented
        };
        match self.kind.as_str() {
            "agent" => same(&self.agent_id, &ctx.actor_agent_id),
            "api_key" => ctx.actor_agent_id.is_none() && same(&self.api_key_id, &ctx.api_key_id),
            "portal_user" => {
                ctx.actor_agent_id.is_none()
                    && ctx.api_key_id.is_none()
                    && if self.user_id.is_some() {
                        same(&self.user_id, &ctx.gateway_user_id)
                    } else {
                        same(&self.idp_user_id, &ctx.idp_user_id)
                    }
            }
            _ => false,
        }
    }
}

/// Canonical form of the arguments an approval covers, for comparison.
///
/// Sorted keys, no whitespace, so the same arguments hash the same whatever
/// order a client serialises them in. `None` and `null` both mean "no
/// arguments were recorded" and compare equal, which keeps surfaces that
/// never pass arguments (A2A capability calls) working unchanged.
fn canonical_arguments(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(value) => {
            let mut out = String::new();
            crate::services::tool_provenance::canonical_json(value, &mut out);
            out
        }
    }
}

/// An approved token, examined and not yet redeemed.
struct ExaminedApproval {
    /// `tool_approval_requests.id`.
    request_id: String,
    /// What the approval was asked for.
    question: Question,
}

/// Examine one presented token without redeeming it: it must exist in this
/// tenant, be `approved`, have been issued to the caller presenting it, and
/// be inside its validity. Returns the approval and the question it answers;
/// whether that question is one the request asks is the caller's decision.
///
/// The question binds the arguments, not merely the tool: the request row
/// has always recorded `call_arguments`, and the approver sees them when
/// they decide, so a token granted for `send_email(to=cfo)` cannot be
/// replayed as `send_email(to=everyone)`.
async fn examine_token(
    db: &sqlx::PgPool,
    token: &str,
    tenant_id: &str,
    // The caller presenting the token.
    caller: &AuthContext,
) -> Result<ExaminedApproval, String> {
    let row: Option<(
        String,
        String,
        String,
        String,
        String,
        Option<Value>,
        Option<chrono::DateTime<chrono::Utc>>,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    )> = sqlx::query_as(
        r#"SELECT id, status, server_name, capability_type, capability_name,
                      call_arguments, resolved_at,
                      requester_type, requester_user_id, requester_api_key_id,
                      request_metadata->>'agent_id',
                      request_metadata->>'requester_idp_user_id',
                      request_metadata->>'bound_to'
               FROM tool_approval_requests
               WHERE approval_token = $1 AND tenant_id = $2"#,
    )
    .bind(token)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    .map_err(|e| format!("DB error: {}", e))?;

    let (
        request_id,
        status,
        req_scope,
        req_cap_type,
        req_cap_name,
        req_args,
        resolved_at,
        requester_type,
        requester_user_id,
        requester_api_key_id,
        requester_agent_id,
        requester_idp_user_id,
        bound_to,
    ) = row.ok_or_else(|| "Token not found".to_string())?;

    if status != "approved" {
        return Err(format!("Request status is '{}', not 'approved'", status));
    }
    // The approval answers the requester's question, so only the
    // requester may spend it.
    let requester = Requester {
        kind: requester_type,
        user_id: requester_user_id,
        api_key_id: requester_api_key_id,
        agent_id: requester_agent_id,
        idp_user_id: requester_idp_user_id,
    };
    if !requester.is(caller) {
        tracing::warn!(
            request_id,
            requester_kind = %requester.kind,
            "approval token presented by a principal other than its requester"
        );
        return Err("Token was issued to a different requester".to_string());
    }

    // An approved token is not valid forever.
    if let Some(approved_at) = resolved_at {
        let age = chrono::Utc::now()
            .signed_duration_since(approved_at)
            .num_seconds();
        if age > APPROVED_TOKEN_TTL_SECS {
            return Err(format!(
                "Approval expired: granted {age}s ago, valid for {APPROVED_TOKEN_TTL_SECS}s"
            ));
        }
    }

    Ok(ExaminedApproval {
        request_id,
        question: Question::bound_to(
            &req_scope,
            &req_cap_type,
            &req_cap_name,
            req_args.as_ref(),
            bound_to.as_deref(),
        ),
    })
}

/// Claim an examined token atomically (one-time use).
///
/// The status predicate puts the decision into the write: exactly one caller
/// can flip `approved` to `consumed`, and whoever sees zero rows affected
/// lost the race and is refused. A SELECT followed by an unconditional
/// UPDATE let two concurrent retries carrying the same token both proceed.
///
/// `token_used` flips in the same write, so it never disagrees with
/// `status`.
async fn claim_token(db: &sqlx::PgPool, request_id: &str) -> Result<(), String> {
    let claimed = sqlx::query(
        r#"UPDATE tool_approval_requests
              SET status = 'consumed', token_used = 'Y', approval_token = NULL
           WHERE id = $1 AND status = 'approved'"#,
    )
    .bind(request_id)
    .execute(db)
    .await
    .map_err(|e| format!("Failed to consume token: {}", e))?;

    if claimed.rows_affected() != 1 {
        return Err("Token already consumed".to_string());
    }
    Ok(())
}

/// Redeem one token for one stated question: the checks of
/// [`require_or_enqueue`] on a request with one gate and one token (examine,
/// compare the question, claim). Returns the redeemed
/// `tool_approval_requests.id`. A token that fails any check is not
/// consumed. Gates go through [`require_or_enqueue`]; this is the same
/// primitives in the order a single gate applies them, for callers that
/// hold a token outside a request.
pub async fn validate_approval_token(
    db: &sqlx::PgPool,
    token: &str,
    tenant_id: &str,
    // The caller presenting the token.
    caller: &AuthContext,
    scope_ref: &str,
    capability_type: &str,
    capability_name: &str,
    // The arguments the retried call actually carries.
    call_arguments: Option<&Value>,
) -> Result<String, String> {
    let approval = examine_token(db, token, tenant_id, caller).await?;
    let asked = Question::of(scope_ref, capability_type, capability_name, call_arguments);
    if approval.question != asked {
        return Err(approval.question.mismatch(&asked).reason().to_string());
    }
    claim_token(db, &approval.request_id).await?;
    Ok(approval.request_id)
}

/// Create an approval request row + approver notifications and return the
/// response envelope as `(StatusCode, JSON)`. The caller wraps it into a
/// `Response` AND captures the JSON for the audit row's
/// `response_body_preview` so the proxy-log row matches the bytes the caller
/// actually saw.
///
/// - Non-interactive callers (API key or agent principal): HTTP **202** with a
///   `poll_url` to retry with `X-Approval-Token`.
/// - Interactive callers (portal JWT user): HTTP **428** Precondition Required.
///
/// `surface` ∈ {`"mcp"`, `"a2a"`, `"skill"`} selects the scope wording and, for
/// MCP only, the `server_config_id` resolution (left NULL for other surfaces).
///
/// `request_metadata` gets caller-supplied fields merged over the
/// `{surface, agent_id}` the resolver relies on (which
/// policy tripped, on which field, against what threshold).
///
/// Private on purpose: the only way to raise a hold is
/// [`require_or_enqueue`], whose [`ApprovalContext`] carries the run the
/// hold belongs to. A public row-writer taking loose `originating_*`
/// arguments is how three surfaces came to raise holds with no run at all.
#[allow(clippy::too_many_arguments)]
async fn create_approval_request_with_metadata(
    db: &sqlx::PgPool,
    tenant_id: &str,
    surface: &str,
    scope_ref: &str,
    capability_type: &str,
    capability_name: &str,
    auth_ctx: &AuthContext,
    // The group the hold is routed to (`approval_window::hold_group_id`) —
    // its members are notified, and its window was resolved.
    group_id: String,
    timeout_seconds: i32,
    call_arguments: Option<&Value>,
    originating_trace_root_id: Option<String>,
    originating_parent_task_id: Option<String>,
    originating_trace_root_kind: Option<String>,
    extra_metadata: Option<Value>,
) -> Result<(StatusCode, Value), String> {
    use chrono::Utc;

    let request_id = ulid::Ulid::new().to_string().to_lowercase();
    let now = Utc::now();
    let expires_at = now + chrono::Duration::seconds(timeout_seconds as i64);

    // Determine requester identity. An agent principal (whether bound via API
    // key or authenticated via a workload token) is recorded as `agent`; a bare
    // API key as `api_key`; a portal session as `portal_user`. The group
    // (`group_id`, resolved by the caller) drives approver routing.

    // `Requester::of` decides the identity; redemption is checked against
    // exactly what is written here.
    let requester = Requester::of(auth_ctx);
    let requester_display = match requester.kind.as_str() {
        "agent" => auth_ctx
            .actor_agent_id
            .clone()
            .unwrap_or_else(|| "Agent".to_string()),
        "api_key" => auth_ctx
            .api_key_name
            .clone()
            .unwrap_or_else(|| "API Key".to_string()),
        _ => auth_ctx
            .gateway_username
            .clone()
            .or_else(|| auth_ctx.idp_username.clone())
            .unwrap_or_else(|| "Unknown".to_string()),
    };
    let requester_type = requester.kind.as_str();
    let requester_user_id = requester.user_id.clone();
    let requester_api_key_id = requester.api_key_id.clone();

    // Resolve server_config_id only for MCP — other surfaces have no analog and
    // the column is nullable.
    let sc_id: Option<String> = if surface == "mcp" {
        sqlx::query_as::<_, (String,)>(
            "SELECT sc.id FROM server_configs sc JOIN mcp_servers ms ON sc.mcp_server_id = ms.id \
             WHERE ms.name = $1 AND sc.tenant_id = $2 AND sc.enabled = true LIMIT 1",
        )
        .bind(scope_ref)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        // Degrade (logged and counted): metadata only; the hold is still raised.
        .or_logged(None)
        .map(|(id,)| id)
    } else {
        None
    };

    let args_json = call_arguments
        .map(|a| serde_json::to_value(a).unwrap_or(Value::Null))
        .unwrap_or(Value::Null);

    let mut request_metadata = json!({
        "surface": surface,
        "agent_id": auth_ctx.actor_agent_id,
        // Who may redeem it, when the requester has no gateway user.
        "requester_idp_user_id": requester.idp_user_id,
    });
    // `surface` and `agent_id` are load-bearing for the decision resolver, so
    // caller-supplied fields are merged in beside them and can never replace
    // them.
    if let (Some(extra), Some(base)) = (
        extra_metadata.as_ref().and_then(|v| v.as_object()),
        request_metadata.as_object_mut(),
    ) {
        for (k, v) in extra {
            if k != "surface" && k != "agent_id" && k != "requester_idp_user_id" {
                base.insert(k.clone(), v.clone());
            }
        }
    }

    // token_used is 'N' until an approval token has been consumed (one-time use).
    sqlx::query(
        r#"INSERT INTO tool_approval_requests
           (id, tenant_id, server_config_id, server_name, capability_type, capability_name,
            requester_type, requester_user_id, requester_api_key_id, requester_group_id,
            requester_display, call_arguments, request_metadata, status, token_used,
            timeout_seconds, expires_at, created_at,
            originating_trace_root_id, originating_parent_task_id,
            originating_trace_root_kind)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'pending', 'N',
                   $14, $15, $16, $17, $18, $19)"#,
    )
    .bind(&request_id)
    .bind(tenant_id)
    .bind(&sc_id)
    .bind(scope_ref)
    .bind(capability_type)
    .bind(capability_name)
    .bind(requester_type)
    .bind(&requester_user_id)
    .bind(&requester_api_key_id)
    .bind(&group_id)
    .bind(&requester_display)
    .bind(&args_json)
    .bind(&request_metadata)
    .bind(timeout_seconds)
    .bind(expires_at)
    .bind(now)
    .bind(&originating_trace_root_id)
    .bind(&originating_parent_task_id)
    .bind(&originating_trace_root_kind)
    .execute(db)
    .await
    .map_err(|e| format!("Failed to insert approval request: {}", e))?;

    // Notify everyone who could approve: the requester (so their own Inbox bell
    // lights up) plus all members of the resource group. Merged via UNION,
    // de-duplicated by DISTINCT.
    let mut member_ids: Vec<i32> = sqlx::query_as::<_, (i32,)>(
        r#"SELECT DISTINCT user_id FROM (
               SELECT user_id FROM resource_group_memberships WHERE group_id = $1
               UNION
               SELECT $2::int AS user_id WHERE $2::int IS NOT NULL
           ) AS approvers WHERE user_id IS NOT NULL"#,
    )
    .bind(&group_id)
    .bind(
        requester_user_id
            .as_deref()
            .and_then(|s| s.parse::<i32>().ok()),
    )
    .fetch_all(db)
    .await
    .map(|rows| rows.into_iter().map(|(uid,)| uid).collect())
    // Degrade (logged and counted): only the approver notifications are
    // lost; the hold itself is already raised.
    .or_logged_default();
    member_ids.sort_unstable();
    member_ids.dedup();

    let scope_noun = match surface {
        "a2a" => "agent",
        "skill" => "skill",
        _ => "server",
    };
    for user_id in &member_ids {
        let notif_id = ulid::Ulid::new().to_string().to_lowercase();
        let _ = sqlx::query(
            r#"INSERT INTO notifications
               (id, tenant_id, user_id, notification_type, title, body, metadata, is_read, created_at)
               VALUES ($1, $2, $3, 'tool_approval_request', $4, $5, $6, FALSE, $7)"#,
        )
        .bind(&notif_id)
        .bind(tenant_id)
        .bind(user_id)
        .bind(format!("Approval needed: {}", capability_name))
        .bind(format!(
            "{} '{}' requested by {} on {} '{}' requires approval.",
            capitalize_first(capability_type),
            capability_name,
            requester_display,
            scope_noun,
            scope_ref
        ))
        .bind(json!({
            "approval_request_id": request_id,
            "tool_name": capability_name,
            "server_name": scope_ref,
            "surface": surface,
            "requester": requester_display,
        }))
        .bind(now)
        .execute(db)
        .await;
    }

    tracing::debug!(
        request_id,
        requester_type,
        surface,
        capability_type,
        capability_name,
        scope_ref,
        timeout_seconds,
        "Created approval request"
    );

    // The row this call writes is the `awaiting_human`
    // record; naming the request on it is what lets the outcome (or the
    // sweeper's `expired` record) chain `supersedes` back to it.
    crate::services::evidence::request::record_enqueued_approval(&request_id);

    let body = json!({
        "approval_required": true,
        "approval_request_id": request_id,
        "capability_type": capability_type,
        "capability_name": capability_name,
        "server_name": scope_ref,
        "surface": surface,
        "timeout_seconds": timeout_seconds,
        "expires_at": expires_at.to_rfc3339(),
        "poll_url": format!("/v1/approvals/{}/poll", request_id),
        "message": format!(
            "{} '{}' requires approval. Submit with X-Approval-Token header after approval.",
            capitalize_first(capability_type),
            capability_name
        ),
    });

    // Non-interactive callers (API key or agent) poll; interactive portal users
    // get the synchronous 428 precondition.
    let non_interactive = auth_ctx.api_key_id.is_some() || auth_ctx.actor_agent_id.is_some();
    let status = if non_interactive {
        StatusCode::ACCEPTED
    } else {
        StatusCode::from_u16(428).unwrap_or(StatusCode::PRECONDITION_FAILED)
    };
    Ok((status, body))
}

fn capitalize_first(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        None => String::new(),
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{HeaderMap, HeaderValue};

    fn headers(values: &[&[u8]]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for v in values {
            h.append(
                crate::constants::headers::X_APPROVAL_TOKEN,
                HeaderValue::from_bytes(v).expect("header value"),
            );
        }
        h
    }

    #[test]
    fn every_header_and_every_comma_separated_value_is_a_token() {
        let a = RequestApprovals::from_headers(&headers(&[b"t1, t2", b"t3", b" ", b"t1"]))
            .with(Some("t4,t2"))
            .with(None);
        assert_eq!(a.tokens, ["t1", "t2", "t3", "t4"]);
        assert!(a.unreadable.is_none());
        assert!(!a.is_empty());
        assert!(RequestApprovals::from_headers(&HeaderMap::new()).is_empty());
    }

    #[test]
    fn a_header_that_cannot_be_read_or_too_many_tokens_is_not_an_empty_set() {
        let a = RequestApprovals::from_headers(&headers(&[b"\xff\xfe"]));
        assert!(a.unreadable.is_some());
        assert!(!a.is_empty(), "a gate must refuse, not raise a new hold");

        let many: Vec<String> = (0..=MAX_APPROVAL_TOKENS).map(|i| format!("t{i}")).collect();
        let a = RequestApprovals::none().with(Some(&many.join(",")));
        assert_eq!(a.tokens.len(), MAX_APPROVAL_TOKENS);
        assert!(a.unreadable.is_some());
    }

    #[test]
    fn a_redeemed_token_is_spent_and_its_question_answered() {
        let a = RequestApprovals::none().with(Some("t1,t2"));
        let q = Question::of("crm", "tool", "refund", Some(&json!({"b": 1, "a": 2})));
        assert!(!a.answered(&q));
        a.redeemed("t1", q.clone());
        assert_eq!(a.unspent(), ["t2"]);
        // The same content in another key order is the same question.
        assert!(a.answered(&Question::of(
            "crm",
            "tool",
            "refund",
            Some(&json!({"a": 2, "b": 1}))
        )));
        // Other content is another question.
        for other in [
            Question::of("crm", "tool", "refund", Some(&json!({"a": 3, "b": 1}))),
            Question::of("crm", "tool", "refund", None),
            Question::of("crm", "tool", "pay", Some(&json!({"a": 2, "b": 1}))),
            Question::of("erp", "tool", "refund", Some(&json!({"a": 2, "b": 1}))),
        ] {
            assert!(!a.answered(&other), "{other:?}");
        }
    }

    #[test]
    fn a_mismatch_names_the_first_part_of_the_question_that_differs() {
        let q = Question::of("crm", "tool", "refund", Some(&json!({"a": 1})));
        let m = |other: &Question| q.mismatch(other);
        assert_eq!(
            m(&Question::of(
                "erp",
                "tool",
                "refund",
                Some(&json!({"a": 1}))
            )),
            Mismatch::Scope
        );
        assert_eq!(
            m(&Question::of("crm", "tool", "pay", Some(&json!({"a": 1})))),
            Mismatch::Capability
        );
        assert_eq!(
            m(&Question::of(
                "crm",
                "tool",
                "refund",
                Some(&json!({"a": 2}))
            )),
            Mismatch::Arguments
        );
        assert!(Mismatch::Arguments.closeness() > Mismatch::Capability.closeness());
        assert!(Mismatch::Capability.closeness() > Mismatch::Scope.closeness());
    }

    fn hold(id: &str) -> (StatusCode, Value, i32) {
        (
            StatusCode::ACCEPTED,
            json!({"approval_request_id": id, "message": "one"}),
            300,
        )
    }

    #[test]
    fn one_hold_is_answered_as_it_always_was() {
        let (status, body) = held_answer(vec![hold("a")], 0);
        assert_eq!(status, StatusCode::ACCEPTED);
        assert_eq!(body, json!({"approval_request_id": "a", "message": "one"}));
    }

    #[test]
    fn several_holds_or_a_kept_token_are_listed_and_counted() {
        let (_, body) = held_answer(vec![hold("a"), hold("b")], 0);
        assert_eq!(body["approval_request_id"], json!("a"));
        assert_eq!(body["approvals_needed"], json!(2));
        assert_eq!(body["approvals"].as_array().map(Vec::len), Some(2));
        assert!(
            !body["message"]
                .as_str()
                .unwrap_or("")
                .contains("stay valid")
        );

        let (_, body) = held_answer(vec![hold("b")], 1);
        assert_eq!(body["approvals_needed"], json!(2));
        assert_eq!(body["approvals"].as_array().map(Vec::len), Some(1));
        assert!(
            body["message"]
                .as_str()
                .unwrap_or("")
                .contains("stay valid")
        );
    }
}
