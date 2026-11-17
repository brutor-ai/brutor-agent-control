// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run segments.
//!
//! Root-plus-`via` keeps ONE run per task, attributed to the
//! system whose credential took the first step, so cost is never counted
//! twice. What it never had is the per-system slice: a
//! shared service that only ever runs *inside* other systems' runs had no
//! numbers of its own, and so could not be assured at all.
//!
//! A **segment** is that slice: one row per AI System that appears in a run
//! other than as its root, computed by the ledger in the same aggregation
//! that finalises the run and written in the same transaction.
//!
//! ## The partition (conservation by construction)
//!
//! The run's steps are read ONCE, grouped by `resource_group_id` (see
//! [`PARTS_SQL`]), inside the same REPEATABLE READ snapshot as the run
//! aggregate. Every group part then lands in exactly one bucket:
//!
//! * a group whose nearest AI System is a system **other than the root** →
//!   that system's segment;
//! * everything else — the root's own groups, a group under no AI System, a
//!   step with no group at all → the root's **own** share.
//!
//! The run's `action_count`, `total_tokens` and `total_cost_usd` are *defined*
//! as the sum over those same parts (`run_ledger::aggregate_run`), so
//! `root-own + Σ segments == run` holds by construction, not by reconciling
//! two computations. Cost is partitioned in integer units of 1e-8 USD — the
//! scale of the `numeric(18,8)` columns — so the identity is exact rather
//! than floating-point-close.
//!
//! Rows under no AI System stay with the root on purpose: that is how the
//! run itself has always been attributed, and a segment is by definition an
//! AI System's share. Nothing is ever dropped.
//!
//! ## Entering calls
//!
//! A delegation *enters* a segment through a step in another bucket: the
//! caller's `a2a` outbound row, whose `current_task_id` is the
//! `parent_task_id` every step the delegate took under the forked chain
//! carries. [`CALLS_SQL`] finds those rows. A delegation that never reached
//! the delegate (timeout, transport error, a gateway refusal before send) has
//! no child steps and therefore no group to resolve; it is attributed through
//! the caller's DECLARED dependency edge on that agent card
//! (`ai_system_dependencies`, role `shared_service`), producing a segment
//! with zero actions. An undeclared card that never answered cannot be
//! attributed to any system and stays a failed step in the caller's share.
//!
//! "A row on the outbound route" is the row's subtype: `outbound`, or a
//! gateway refusal subtype ([`UNANSWERED_CALL_SUBTYPES`]) on a row the
//! outbound route wrote in the run — the run controls' refusals and the
//! accepted-caller refusal (`unaccepted_caller`) stamp their
//! own subtype instead of `outbound`, and are still delegations the caller
//! attempted. Inbound refusal rows (the service's side) are excluded by the
//! route: they are not the caller's attempt.
//!
//! ## Outcome mapping
//!
//! Each entering call is classified from what the CALLER received, in this
//! order (first match wins):
//!
//! | condition on the entering call                                   | outcome     |
//! |------------------------------------------------------------------|-------------|
//! | held for a human (`proxy_subtype = approval_required`)           | *held* — not an outcome; see below |
//! | failed, and the service's last act (no later) was a hold — its gateway held the call on the inbound side | `failed`, refused, held (see below) |
//! | guardrail block, argument-policy denial, or a gateway refusal subtype ([`GATEWAY_REFUSAL_SUBTYPES`]) | `failed`, refused (`error_cause = blocked_by_policy`) |
//! | not successful and HTTP 408/504, or the error says it timed out   | `timed_out` |
//! | not successful otherwise (transport error, non-2xx)              | `failed`    |
//! | 2xx carrying a JSON-RPC `error` object                           | `failed`    |
//! | A2A task state (`proxy_logs.a2a_task_state`) `failed` or `canceled` | `failed` |
//! | A2A task state `rejected` or `auth_required`                     | `rejected`  |
//! | anything else (`completed`, `working`, `submitted`, `input_required`, or a plain message reply) | `answered` |
//!
//! A segment entered more than once takes the outcome of its LATEST call —
//! the answer the caller was finally left with, so a retry that succeeded
//! after a timeout reads as answered. The control plane classifies these
//! outcomes the same way (`classify_segment_outcome` in
//! `app/services/ai_system_down_rule.py`: `answered` and `rejected` are
//! answered — the delegate declined, it did not fail; `failed` and
//! `timed_out` are unanswered; a `failed` segment whose `error_cause` is
//! `blocked_by_policy` is the gateway's refusal, not the delegate's).
//! Its "down" judgement is ONE rule over the latest THREE
//! calls, not the latest one: all three unanswered = down (silent), any one
//! unanswered = degraded. On-demand liveness, dependency health and the
//! connection lines all read it; nothing in the core decides "down".
//! Held calls are skipped (the retry that follows the approval is the
//! answer); a segment entered ONLY by held calls is `failed` with
//! `error_cause = blocked_by_policy` — governance stopped it pending a person.
//! Such a segment also records the approval request it waits on
//! (`held_approval_request_id`, the latest held call's
//! `custom_metadata.approval_request_id`), as does a segment no call entered
//! whose last act was a hold. A hold is counted apart from a refusal:
//! the control plane judges it by that request — decided
//! or still inside its timeout is all good, lapsed is a warning. The stored
//! outcome and cause stay as they were, so a reader that does not know the
//! column still reads a gateway stop, never a delegate failure.
//!
//! A segment with no entering call in another bucket (the delegate joined the
//! run by asserting its id rather than through a forked chain) is classified
//! from its own last step: blocked/refused → `failed` + `blocked_by_policy`,
//! failed → `failed`, otherwise `answered`.
//!
//! ## Delegated outcomes
//!
//! The outbound A2A route stores the delegate's reported outcome
//! (`X-Brutor-Outcome-Id` on its reply, or `metadata["brutor.outcome.id"]` on
//! the final Task) on its own row's `proxy_logs.outcome_id`. A segment's
//! reported outcome is that of the call that decided its outcome (the latest
//! non-held entering call), else the latest one the segment's own steps
//! reported. It is judged against the SEGMENT system's active contract
//! ([`crate::services::run_outcome`]) — never the caller's — and its
//! `error_cause` comes from [`crate::services::run_outcome::segment_error_cause`].
//! A segment that no A2A call entered and whose steps reported nothing is
//! not judged (a shared MCP service has no way to report an outcome).
//!
//! `a2a_task_state` is the task state carried by the LAST entering call
//! whose row names one. The ledger never parses a reply for it: the gateway
//! wrote `proxy_logs.a2a_task_state` from the bytes the caller received
//! ([`crate::services::a2a_task_state`], the single source — a stored body
//! may since have been pruned or reduced to metadata). The one fact still
//! read from the stored body is whether a 2xx was a JSON-RPC error envelope,
//! which is not a task state. `caller_agent_id` is the actor on the FIRST
//! entering call — the agent whose delegation entered the segment.
//!
//! `caller_system_id` is the IMMEDIATE calling AI System: the nearest
//! AI System of that same first entering call's group, from the same
//! `group_system` map the partition buckets with — built by
//! [`crate::services::ai_system::nearest_ai_systems`], the resolver the
//! accepted-caller gate uses too. In A→B→C, C's caller is B; `root_system_id`
//! stays the run's root. NULL when the calling step is in no AI System. A
//! segment no call entered has no delegating step to resolve (a step in
//! another bucket parenting it would have been an entering call), so its
//! caller is NULL as well.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{DateTime, Utc};
use serde_json::json;
use sqlx::{FromRow, PgConnection, PgPool, Postgres, Transaction};

use crate::constants::{proxy_subtype, routes};
use crate::services::a2a_task_state;
use crate::services::run_ledger::{held_ms_sql, tool_set_signature_sql};

/// Wire contract for `ai_system_run_segments.outcome`, mirrored in
/// `app/database/models/ai_system_run_segment.py` (`SEGMENT_OUTCOMES`).
pub mod outcome {
    pub const ANSWERED: &str = "answered";
    pub const FAILED: &str = "failed";
    pub const REJECTED: &str = "rejected";
    pub const TIMED_OUT: &str = "timed_out";
    /// There is no `blocked`: a call the gateway refused is `failed`
    /// with the segment's `error_cause = blocked_by_policy`.
    pub const ALL: [&str; 4] = [ANSWERED, FAILED, REJECTED, TIMED_OUT];

    /// The outcomes that mean the service did not answer — the only ones
    /// that make an on-demand service silent. Mirrors
    /// `SEGMENT_UNANSWERED_OUTCOMES` in the control plane.
    pub const UNANSWERED: [&str; 2] = [FAILED, TIMED_OUT];
}

/// Integer cost units per USD — the scale of `numeric(18,8)`.
pub const COST_UNITS_PER_USD: i64 = 100_000_000;

pub fn units_to_usd(units: i64) -> f64 {
    units as f64 / COST_UNITS_PER_USD as f64
}

/// Proxy subtypes that mean the GATEWAY refused the call — a governance
/// outcome, not a failure of the service being called.
pub const GATEWAY_REFUSAL_SUBTYPES: [&str; 13] = [
    proxy_subtype::AGENT_AUTHZ_BLOCK,
    proxy_subtype::SEMANTIC_POLICY_BLOCK,
    proxy_subtype::APPROVAL_REJECTED,
    proxy_subtype::APPROVAL_TOKEN_INVALID,
    proxy_subtype::RUN_ABORTED,
    proxy_subtype::RUN_CAP_REACHED,
    proxy_subtype::ACCESS_DENIED,
    proxy_subtype::RESIDENCY_VIOLATION,
    proxy_subtype::AUTONOMY_BLOCK,
    proxy_subtype::SKILL_QUOTA_EXCEEDED,
    proxy_subtype::SKILL_CONCURRENCY_LIMIT,
    proxy_subtype::UNACCEPTED_CALLER,
    proxy_subtype::LLM_GOVERNANCE_BLOCK,
];

/// Subtypes of a row the OUTBOUND route wrote for a delegation the caller
/// attempted: the call itself, a hold for a person before it left (a
/// held delegation is the delegate's segment, counted
/// as a hold), or a gateway refusal that
/// stopped it before it left. Bound as `$3` of [`CALLS_SQL`] and read by
/// [`is_unanswered_outbound`], so the SQL and the partition agree.
pub const UNANSWERED_CALL_SUBTYPES: [&str; GATEWAY_REFUSAL_SUBTYPES.len() + 2] = {
    let mut out = [proxy_subtype::OUTBOUND; GATEWAY_REFUSAL_SUBTYPES.len() + 2];
    out[1] = proxy_subtype::APPROVAL_REQUIRED;
    let mut i = 0;
    while i < GATEWAY_REFUSAL_SUBTYPES.len() {
        out[i + 2] = GATEWAY_REFUSAL_SUBTYPES[i];
        i += 1;
    }
    out
};

// ---------------------------------------------------------------------------
// Snapshot inputs — read in the run aggregate's REPEATABLE READ snapshot
// ---------------------------------------------------------------------------

/// One resource group's share of a run's steps. The unit of the partition:
/// every step belongs to exactly one part (NULL group included).
#[derive(Debug, Clone, Default, FromRow)]
pub struct GroupPart {
    pub group_id: Option<String>,
    pub action_count: i64,
    pub llm_call_count: i64,
    pub tool_call_count: i64,
    pub error_count: i64,
    pub tool_error_count: i64,
    pub guardrail_block_count: i64,
    pub policy_denial_count: i64,
    pub approval_request_count: i64,
    pub total_tokens: i64,
    /// Cost in [`COST_UNITS_PER_USD`] units, rounded once per part.
    pub cost_units: i64,
    pub min_depth: i32,
    pub first_at: DateTime<Utc>,
    pub last_at: DateTime<Utc>,
    /// When the part's earliest call STARTED (rows are stamped on completion;
    /// see `run_ledger::step_started_at_sql!`). The start of active time.
    pub active_from: DateTime<Utc>,
    /// Distinct tool/capability names this part called.
    pub tools: Vec<String>,
    /// One entry per model call (so the multiset carries the call counts).
    pub models: Vec<String>,
    // The part's last step, for classifying a segment no call entered.
    pub last_id: String,
    pub last_success: bool,
    pub last_blocked: bool,
    pub last_subtype: Option<String>,
    /// The last step's `custom_metadata.approval_request_id` (set when it was
    /// held for a person).
    pub last_approval_request_id: Option<String>,
    /// The latest outcome id this part's steps reported (the
    /// outbound A2A route's rows excluded: theirs is the DELEGATE's), and
    /// when.
    pub last_outcome_id: Option<String>,
    pub last_outcome_at: Option<DateTime<Utc>>,
}

/// A step whose own task id is the parent of a step in ANOTHER group, or an
/// A2A outbound call that no step answered. The candidates for "the call that
/// entered a segment"; [`partition`] decides which ones actually cross a
/// system boundary.
#[derive(Debug, Clone, Default, FromRow)]
pub struct DelegationCall {
    pub id: String,
    pub at: DateTime<Utc>,
    pub group_id: Option<String>,
    /// The agent card id on an `a2a` row.
    pub server_id: Option<String>,
    pub proxy_type: String,
    pub proxy_subtype: Option<String>,
    pub actor_agent_id: Option<String>,
    pub success: bool,
    pub http_status_code: Option<i32>,
    pub error_message: Option<String>,
    pub guardrail_blocked: bool,
    pub argument_policy_denied: bool,
    pub depth: i32,
    /// Stored reply — read ONLY for the JSON-RPC error envelope.
    pub response_body: Option<String>,
    /// `proxy_logs.a2a_task_state`: the task state the caller received.
    pub a2a_task_state: Option<String>,
    /// On an outbound A2A row, the outcome the DELEGATE reported.
    pub outcome_id: Option<String>,
    /// `custom_metadata.approval_request_id`: the request a held call waits on.
    pub approval_request_id: Option<String>,
    /// Distinct non-null groups of the steps this call parented, excluding
    /// the call's own group.
    pub child_groups: Vec<String>,
    pub has_children: bool,
}

/// Per-group partial aggregates. `$1` tenant, `$2` root_task_id, `$3` the
/// tool proxy types. The count expressions are the run aggregate's own
/// ([`crate::services::run_ledger`]'s `step_tally_sql!`), so a segment's
/// `error_count` means exactly what the run's does.
pub const PARTS_SQL: &str = concat!(
    r#"
SELECT
    pl.resource_group_id                                       AS group_id,
    COUNT(*)                                                   AS action_count,
    COALESCE(SUM(pl.llm_total_tokens), 0)                      AS total_tokens,
    -- Rounded to the column scale ONCE per part; the run total is the sum of
    -- these integers, which is what makes conservation exact.
    CAST(ROUND(COALESCE(SUM(CAST(pl.llm_estimated_cost_usd AS NUMERIC)), 0) * 100000000) AS BIGINT)
                                                               AS cost_units,
"#,
    crate::services::run_ledger::step_tally_sql!(),
    r#"
    CAST(COALESCE(MIN(pl.delegation_depth), 0) AS INTEGER)     AS min_depth,
    MIN(pl.timestamp)                                          AS first_at,
    MAX(pl.timestamp)                                          AS last_at,
    MIN("#,
    crate::services::run_ledger::step_started_at_sql!(),
    r#")                                                       AS active_from,
    COALESCE(ARRAY_AGG(DISTINCT pl.mcp_capability_name)
        FILTER (WHERE pl.mcp_capability_name IS NOT NULL), '{}') AS tools,
    COALESCE(ARRAY_AGG(pl.llm_model_used)
        FILTER (WHERE pl.llm_model_used IS NOT NULL), '{}')    AS models,
    (ARRAY_AGG(pl.id ORDER BY pl.timestamp DESC, pl.id DESC))[1] AS last_id,
    (ARRAY_AGG(pl.success ORDER BY pl.timestamp DESC, pl.id DESC))[1] AS last_success,
    (ARRAY_AGG(pl.guardrail_blocked OR pl.argument_policy_denied
               ORDER BY pl.timestamp DESC, pl.id DESC))[1]     AS last_blocked,
    (ARRAY_AGG(pl.proxy_subtype ORDER BY pl.timestamp DESC, pl.id DESC))[1] AS last_subtype,
    (ARRAY_AGG(pl.custom_metadata ->> 'approval_request_id'
               ORDER BY pl.timestamp DESC, pl.id DESC))[1]     AS last_approval_request_id,
    -- The latest outcome this part's steps reported. A row the
    -- outbound A2A route wrote carries the DELEGATE's outcome, never this
    -- part's own, so it is excluded here (it reaches the delegate's segment
    -- through the entering call instead).
    (ARRAY_AGG(pl.outcome_id ORDER BY pl.timestamp DESC, pl.id DESC)
        FILTER (WHERE pl.outcome_id IS NOT NULL
                  AND NOT (pl.proxy_type = 'a2a'
                           AND pl.endpoint = '/v1/proxy/a2a/outbound')))[1] AS last_outcome_id,
    MAX(pl.timestamp) FILTER (WHERE pl.outcome_id IS NOT NULL
                  AND NOT (pl.proxy_type = 'a2a'
                           AND pl.endpoint = '/v1/proxy/a2a/outbound')) AS last_outcome_at
FROM proxy_logs pl
WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
GROUP BY pl.resource_group_id
"#
);

/// Candidate entering calls. `$1` tenant, `$2` root_task_id, `$3`
/// [`UNANSWERED_CALL_SUBTYPES`], `$4` the outbound route.
///
/// Several rows can share one `current_task_id` (a semantic-policy shadow
/// row precedes the outbound call's outcome row); the LAST one is what the
/// caller received.
pub const CALLS_SQL: &str = r#"
WITH steps AS (
    SELECT id, timestamp, resource_group_id, server_id, proxy_type, proxy_subtype,
           actor_agent_id, success, http_status_code, error_message,
           guardrail_blocked, argument_policy_denied, delegation_depth,
           response_body_preview, a2a_task_state, outcome_id, endpoint,
           current_task_id, parent_task_id,
           custom_metadata ->> 'approval_request_id' AS approval_request_id
      FROM proxy_logs
     WHERE tenant_id = $1 AND root_task_id = $2
),
calls AS (
    SELECT DISTINCT ON (s.current_task_id) s.*
      FROM steps s
     WHERE s.current_task_id IS NOT NULL
     ORDER BY s.current_task_id, s.timestamp DESC, s.id DESC
),
-- One pass over the children, joined by hash: a correlated lookup per call
-- would be quadratic in the length of the run.
children AS (
    SELECT parent_task_id,
           ARRAY_AGG(DISTINCT resource_group_id)
               FILTER (WHERE resource_group_id IS NOT NULL) AS groups
      FROM steps
     WHERE parent_task_id IS NOT NULL
     GROUP BY parent_task_id
),
linked AS (
    SELECT c.*,
           COALESCE(ARRAY(
               SELECT g FROM UNNEST(ch.groups) AS g
                WHERE g IS DISTINCT FROM c.resource_group_id
           ), '{}')                                              AS child_groups,
           ch.parent_task_id IS NOT NULL                         AS has_children
      FROM calls c
      LEFT JOIN children ch ON ch.parent_task_id = c.current_task_id
)
SELECT id,
       timestamp                                   AS at,
       resource_group_id                           AS group_id,
       server_id,
       proxy_type,
       proxy_subtype,
       actor_agent_id,
       success,
       http_status_code,
       error_message,
       guardrail_blocked,
       argument_policy_denied,
       COALESCE(delegation_depth, 0)               AS depth,
       response_body_preview                       AS response_body,
       a2a_task_state,
       outcome_id,
       approval_request_id,
       child_groups,
       has_children
  FROM linked
 WHERE CARDINALITY(child_groups) > 0
    OR (proxy_type = 'a2a' AND proxy_subtype = ANY($3) AND endpoint = $4
        AND NOT has_children)
 ORDER BY timestamp, id
"#;

pub async fn load_parts(
    conn: &mut PgConnection,
    tenant_id: &str,
    root_task_id: &str,
    tool_proxy_types: &[&str],
) -> Result<Vec<GroupPart>, sqlx::Error> {
    sqlx::query_as::<_, GroupPart>(PARTS_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(tool_proxy_types)
        .fetch_all(conn)
        .await
}

pub async fn load_calls(
    conn: &mut PgConnection,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Vec<DelegationCall>, sqlx::Error> {
    sqlx::query_as::<_, DelegationCall>(CALLS_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(&UNANSWERED_CALL_SUBTYPES[..])
        .bind(routes::A2A_OUTBOUND)
        .fetch_all(conn)
        .await
}

/// The declared `shared_service` targets of the agent cards these calls
/// named, keyed `(calling system, card) → target system`. Used only for a
/// call that no step answered — every other call's target is observed.
pub async fn load_card_targets(
    db: &PgPool,
    tenant_id: &str,
    calls: &[DelegationCall],
) -> Result<HashMap<(String, String), String>, sqlx::Error> {
    let cards: Vec<String> = calls
        .iter()
        .filter(|c| is_unanswered_outbound(c))
        .filter_map(|c| c.server_id.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if cards.is_empty() {
        return Ok(HashMap::new());
    }
    let rows: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT ai_system_id, agent_card_id, depends_on_system_id \
           FROM ai_system_dependencies \
          WHERE tenant_id = $1 AND role = 'shared_service' \
            AND agent_card_id = ANY($2) AND depends_on_system_id IS NOT NULL",
    )
    .bind(tenant_id)
    .bind(&cards)
    .fetch_all(db)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(caller, card, target)| ((caller, card), target))
        .collect())
}

// ---------------------------------------------------------------------------
// The partition — pure, so the invariant can be property-tested
// ---------------------------------------------------------------------------

/// The additive measures a partition splits. `action_count`, `total_tokens`
/// and `cost_units` are the conserved three.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Tally {
    pub action_count: i64,
    pub llm_call_count: i64,
    pub tool_call_count: i64,
    pub error_count: i64,
    pub tool_error_count: i64,
    pub guardrail_block_count: i64,
    pub policy_denial_count: i64,
    pub approval_request_count: i64,
    pub total_tokens: i64,
    pub cost_units: i64,
}

impl Tally {
    pub fn add_part(&mut self, p: &GroupPart) {
        self.action_count += p.action_count;
        self.llm_call_count += p.llm_call_count;
        self.tool_call_count += p.tool_call_count;
        self.error_count += p.error_count;
        self.tool_error_count += p.tool_error_count;
        self.guardrail_block_count += p.guardrail_block_count;
        self.policy_denial_count += p.policy_denial_count;
        self.approval_request_count += p.approval_request_count;
        self.total_tokens += p.total_tokens;
        self.cost_units += p.cost_units;
    }

    pub fn add(&mut self, o: &Tally) {
        self.action_count += o.action_count;
        self.llm_call_count += o.llm_call_count;
        self.tool_call_count += o.tool_call_count;
        self.error_count += o.error_count;
        self.tool_error_count += o.tool_error_count;
        self.guardrail_block_count += o.guardrail_block_count;
        self.policy_denial_count += o.policy_denial_count;
        self.approval_request_count += o.approval_request_count;
        self.total_tokens += o.total_tokens;
        self.cost_units += o.cost_units;
    }

    /// The run's totals: the sum over EVERY part. `aggregate_run` stores these
    /// as the run's `action_count` / `total_tokens` / `total_cost_usd`.
    pub fn of_parts(parts: &[GroupPart]) -> Tally {
        let mut t = Tally::default();
        for p in parts {
            t.add_part(p);
        }
        t
    }
}

/// One segment as the partition derives it — every column but the three
/// that need the database (`tool_set_signature`, `active_ms`, contract).
#[derive(Debug, Clone, PartialEq)]
pub struct SegmentDraft {
    pub system_id: String,
    /// Groups whose steps this segment holds (its holds are filtered to them).
    pub group_ids: Vec<String>,
    pub tally: Tally,
    pub min_depth: i32,
    /// The first step's stamp (when it finished): orders segments and is
    /// what "when" means on a segment.
    pub first_action_at: DateTime<Utc>,
    pub last_action_at: DateTime<Utc>,
    /// When the segment's earliest call STARTED — the start of its active
    /// time. `<= first_action_at`; equal for a segment no step answered.
    pub active_from: DateTime<Utc>,
    pub tools: Vec<String>,
    pub models_used: BTreeMap<String, i64>,
    pub caller_agent_id: Option<String>,
    /// The immediate calling AI System (see the module docs).
    pub caller_system_id: Option<String>,
    pub a2a_task_state: Option<String>,
    pub outcome: &'static str,
    /// The gateway refused the call that decided the outcome (or
    /// only held calls entered): `error_cause = blocked_by_policy`.
    pub refused: bool,
    /// Stopped by a hold, not a refusal: the approval request it waits on
    /// (module docs). `Some` implies `refused`.
    pub held_approval_request_id: Option<String>,
    /// Ids of the calls that entered the segment, chronological.
    pub entered_by: Vec<String>,
    /// At least one entering call was an A2A delegation: the
    /// segment's system is a delegate that owes the caller an outcome.
    pub entered_by_a2a: bool,
    /// The outcome the delegate reported (see the module docs).
    pub reported_outcome_id: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct RunPartition {
    /// The root system's own share: its groups, unmapped groups, no group.
    pub root_own: Tally,
    /// The latest outcome the root's OWN steps reported. A
    /// delegate's outcome (its segment's steps, or the outbound A2A row that
    /// relayed it) is never the run's.
    pub root_outcome_id: Option<String>,
    /// Ordered by `(first_action_at, system_id)` — stable across reopens.
    pub segments: Vec<SegmentDraft>,
}

impl RunPartition {
    pub fn totals(&self) -> Tally {
        let mut t = self.root_own;
        for s in &self.segments {
            t.add(&s.tally);
        }
        t
    }

    /// `via_system_ids`, derived from the segment set — the single source.
    pub fn via(&self) -> Vec<String> {
        self.segments.iter().map(|s| s.system_id.clone()).collect()
    }
}

#[derive(Default)]
struct Builder<'a> {
    parts: Vec<&'a GroupPart>,
    calls: Vec<&'a DelegationCall>,
}

fn is_unanswered_outbound(c: &DelegationCall) -> bool {
    c.proxy_type == crate::constants::proxy::A2A
        && c.proxy_subtype
            .as_deref()
            .is_some_and(|s| UNANSWERED_CALL_SUBTYPES.contains(&s))
        && !c.has_children
}

/// Split a run into the root's own share and one segment per other AI System.
///
/// * `root_system` — the run's attributed system (`None` when unattributed:
///   then every AI System in the run is a segment).
/// * `group_system` — nearest AI System per group; absent = under none.
/// * `card_targets` — declared `(caller system, card) → target system`.
pub fn partition(
    parts: &[GroupPart],
    calls: &[DelegationCall],
    root_system: Option<&str>,
    group_system: &HashMap<String, String>,
    card_targets: &HashMap<(String, String), String>,
) -> RunPartition {
    let bucket_of = |group: Option<&str>| -> Option<&str> {
        group
            .and_then(|g| group_system.get(g))
            .map(String::as_str)
            .filter(|s| Some(*s) != root_system)
    };

    let mut root_own = Tally::default();
    let mut root_outcome: Option<(DateTime<Utc>, &str)> = None;
    let mut builders: BTreeMap<&str, Builder<'_>> = BTreeMap::new();

    // Every part lands in exactly one bucket — the whole invariant.
    for p in parts {
        match bucket_of(p.group_id.as_deref()) {
            Some(sys) => builders.entry(sys).or_default().parts.push(p),
            None => {
                root_own.add_part(p);
                if let (Some(id), Some(at)) = (p.last_outcome_id.as_deref(), p.last_outcome_at) {
                    if root_outcome.is_none_or(|(best, _)| at > best) {
                        root_outcome = Some((at, id));
                    }
                }
            }
        }
    }

    for c in calls {
        let caller = bucket_of(c.group_id.as_deref());
        let caller_system = caller.or(root_system);
        let mut targets: BTreeSet<&str> = BTreeSet::new();
        if c.has_children {
            for g in &c.child_groups {
                if let Some(sys) = bucket_of(Some(g.as_str())) {
                    if Some(sys) != caller {
                        targets.insert(sys);
                    }
                }
            }
        } else if is_unanswered_outbound(c) {
            if let (Some(from), Some(card)) = (caller_system, c.server_id.as_deref()) {
                if let Some(to) = card_targets.get(&(from.to_string(), card.to_string())) {
                    if Some(to.as_str()) != root_system && to != from {
                        targets.insert(to.as_str());
                    }
                }
            }
        }
        for t in targets {
            builders.entry(t).or_default().calls.push(c);
        }
    }

    let mut segments: Vec<SegmentDraft> = builders
        .into_iter()
        .map(|(sys, b)| build_segment(sys, b, group_system))
        .collect();
    segments
        .sort_by(|a, b| (a.first_action_at, &a.system_id).cmp(&(b.first_action_at, &b.system_id)));
    RunPartition {
        root_own,
        root_outcome_id: root_outcome.map(|(_, id)| id.to_string()),
        segments,
    }
}

fn build_segment(
    system_id: &str,
    mut b: Builder<'_>,
    group_system: &HashMap<String, String>,
) -> SegmentDraft {
    b.calls.sort_by(|x, y| (x.at, &x.id).cmp(&(y.at, &y.id)));
    b.calls.dedup_by(|x, y| x.id == y.id);

    let mut tally = Tally::default();
    let mut tools: BTreeSet<String> = BTreeSet::new();
    let mut models_used: BTreeMap<String, i64> = BTreeMap::new();
    let mut group_ids: Vec<String> = Vec::new();
    for p in &b.parts {
        tally.add_part(p);
        tools.extend(p.tools.iter().cloned());
        for m in &p.models {
            *models_used.entry(m.clone()).or_insert(0) += 1;
        }
        if let Some(g) = &p.group_id {
            group_ids.push(g.clone());
        }
    }
    group_ids.sort();

    let (first, last, active_from, min_depth) = if b.parts.is_empty() {
        // Entered but never answered: the segment exists because the call
        // was made, at the depth the delegate would have run at. The
        // delegate did no work of its own, so it has no active time.
        let first = b.calls.first().map(|c| c.at).unwrap_or_default();
        let last = b.calls.last().map(|c| c.at).unwrap_or_default();
        let depth = b.calls.iter().map(|c| c.depth).min().unwrap_or(0) + 1;
        (first, last, first, depth)
    } else {
        let first = b.parts.iter().map(|p| p.first_at).min().unwrap_or_default();
        (
            first,
            b.parts.iter().map(|p| p.last_at).max().unwrap_or_default(),
            // Never later than the first stamp, whatever a part reports.
            b.parts
                .iter()
                .map(|p| p.active_from)
                .min()
                .map_or(first, |a| a.min(first)),
            b.parts.iter().map(|p| p.min_depth).min().unwrap_or(0),
        )
    };

    let (outcome, a2a_task_state, refused, decided_by, held_approval_request_id) =
        if b.calls.is_empty() {
            let last_part = b
                .parts
                .iter()
                .max_by(|x, y| (x.last_at, &x.last_id).cmp(&(y.last_at, &y.last_id)));
            let (o, r) = last_part.map_or((outcome::ANSWERED, false), |p| own_outcome(p));
            (
                o,
                None,
                r,
                None,
                last_part.and_then(|p| own_held_approval_request_id(p)),
            )
        } else {
            let (o, state, r) = segment_outcome(&b.calls);
            let last_part = b
                .parts
                .iter()
                .max_by(|x, y| (x.last_at, &x.last_id).cmp(&(y.last_at, &y.last_id)));
            match inbound_hold(&b.calls, last_part.copied()) {
                // The service's gateway held the call on its inbound side:
                // governance stopped it, the service did not fail to answer.
                Some(id) => (
                    outcome::FAILED,
                    state,
                    true,
                    deciding_call(&b.calls),
                    Some(id),
                ),
                None => {
                    let held = held_approval_request_id(&b.calls);
                    (o, state, r, deciding_call(&b.calls), held)
                }
            }
        };
    // The delegate's reported outcome: on the call that decided the outcome,
    // else the latest one the segment's own steps reported.
    let reported_outcome_id = decided_by.and_then(|c| c.outcome_id.clone()).or_else(|| {
        b.parts
            .iter()
            .filter(|p| p.last_outcome_id.is_some())
            .max_by_key(|p| p.last_outcome_at)
            .and_then(|p| p.last_outcome_id.clone())
    });
    let entered_by_a2a = b
        .calls
        .iter()
        .any(|c| c.proxy_type == crate::constants::proxy::A2A);

    SegmentDraft {
        system_id: system_id.to_string(),
        group_ids,
        tally,
        min_depth,
        first_action_at: first,
        last_action_at: last,
        active_from,
        tools: tools.into_iter().collect(),
        models_used,
        caller_agent_id: b.calls.first().and_then(|c| c.actor_agent_id.clone()),
        caller_system_id: b
            .calls
            .first()
            .and_then(|c| c.group_id.as_deref())
            .and_then(|g| group_system.get(g))
            .cloned(),
        a2a_task_state,
        outcome,
        refused,
        held_approval_request_id,
        entered_by: b.calls.iter().map(|c| c.id.clone()).collect(),
        entered_by_a2a,
        reported_outcome_id,
    }
}

// ---------------------------------------------------------------------------
// Outcome — see the module docs for the table
// ---------------------------------------------------------------------------

/// What one entering call amounted to for the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallVerdict {
    /// Parked on a human approval: not an answer yet.
    Held,
    /// The GATEWAY refused it (outcome `failed`, segment
    /// `error_cause = blocked_by_policy` — never the delegate's failure).
    Refused,
    Outcome(&'static str),
}

fn is_refusal(subtype: Option<&str>) -> bool {
    subtype.is_some_and(|s| GATEWAY_REFUSAL_SUBTYPES.contains(&s))
}

/// The call whose verdict decided the segment's outcome: the latest non-held
/// one. `calls` is chronological.
fn deciding_call<'c>(calls: &[&'c DelegationCall]) -> Option<&'c DelegationCall> {
    calls
        .iter()
        .rev()
        .find(|c| classify_call(c) != CallVerdict::Held)
        .copied()
}

fn looks_timed_out(c: &DelegationCall) -> bool {
    if matches!(c.http_status_code, Some(408) | Some(504)) {
        return true;
    }
    c.error_message.as_deref().is_some_and(|m| {
        let m = m.to_ascii_lowercase();
        m.contains("timed out") || m.contains("timeout")
    })
}

pub fn classify_call(c: &DelegationCall) -> CallVerdict {
    if c.proxy_subtype.as_deref() == Some(proxy_subtype::APPROVAL_REQUIRED) {
        return CallVerdict::Held;
    }
    if c.guardrail_blocked || c.argument_policy_denied || is_refusal(c.proxy_subtype.as_deref()) {
        return CallVerdict::Refused;
    }
    if !c.success {
        return CallVerdict::Outcome(if looks_timed_out(c) {
            outcome::TIMED_OUT
        } else {
            outcome::FAILED
        });
    }
    if a2a_task_state::is_jsonrpc_error(c.response_body.as_deref()) {
        return CallVerdict::Outcome(outcome::FAILED);
    }
    CallVerdict::Outcome(match c.a2a_task_state.as_deref() {
        Some("failed") | Some("canceled") => outcome::FAILED,
        Some("rejected") | Some("auth_required") => outcome::REJECTED,
        _ => outcome::ANSWERED,
    })
}

/// The latest non-held call's outcome, the last task state the caller was
/// handed, and whether the gateway refused it. `calls` is chronological and
/// non-empty.
pub fn segment_outcome(calls: &[&DelegationCall]) -> (&'static str, Option<String>, bool) {
    let state = calls.iter().rev().find_map(|c| c.a2a_task_state.clone());
    match deciding_call(calls).map(classify_call) {
        Some(CallVerdict::Outcome(o)) => (o, state, false),
        Some(CallVerdict::Refused) => (outcome::FAILED, state, true),
        // Only held calls: governance parked it and no answer ever followed.
        Some(CallVerdict::Held) | None => (outcome::FAILED, state, true),
    }
}

/// The approval request a segment entered ONLY by held calls waits on: the
/// latest held call's. `None` when any call decided the outcome (an answer,
/// a failure or a refusal), or the held call names no request (it then reads
/// as the refusal it is stored as — never better than the evidence).
pub fn held_approval_request_id(calls: &[&DelegationCall]) -> Option<String> {
    if deciding_call(calls).is_some() {
        return None;
    }
    calls
        .iter()
        .rev()
        .find(|c| classify_call(c) == CallVerdict::Held)
        .and_then(|c| c.approval_request_id.clone())
}

/// The caller's deciding call FAILED and the service's last act — no later
/// than that call — was a hold: the service's gateway held the call on its
/// inbound side (the caller agent's `a2a_call` grant needs a person) and
/// answered the caller with a refusal. Returns the request it waits on.
pub fn inbound_hold(calls: &[&DelegationCall], last_part: Option<&GroupPart>) -> Option<String> {
    let call = deciding_call(calls)?;
    if classify_call(call) != CallVerdict::Outcome(outcome::FAILED) {
        return None;
    }
    let p = last_part?;
    if p.last_at > call.at {
        return None;
    }
    own_held_approval_request_id(p)
}

/// A segment no call entered whose last act was a hold: the request it
/// waits on.
pub fn own_held_approval_request_id(p: &GroupPart) -> Option<String> {
    if p.last_blocked || is_refusal(p.last_subtype.as_deref()) {
        return None;
    }
    (p.last_subtype.as_deref() == Some(proxy_subtype::APPROVAL_REQUIRED))
        .then(|| p.last_approval_request_id.clone())
        .flatten()
}

/// A segment no call entered: judged by its own last step, as
/// `(outcome, refused)`.
fn own_outcome(p: &GroupPart) -> (&'static str, bool) {
    if p.last_blocked || is_refusal(p.last_subtype.as_deref()) {
        (outcome::FAILED, true)
    } else if p.last_subtype.as_deref() == Some(proxy_subtype::APPROVAL_REQUIRED) {
        // Its last act was to wait for a person — governance, not failure.
        (outcome::FAILED, true)
    } else if !p.last_success {
        (outcome::FAILED, false)
    } else {
        (outcome::ANSWERED, false)
    }
}

// ---------------------------------------------------------------------------
// Finishing (database) + writing
// ---------------------------------------------------------------------------

/// A segment ready to write.
#[derive(Debug, Clone)]
pub struct Segment {
    pub draft: SegmentDraft,
    pub tool_set_signature: Option<String>,
    pub active_ms: i64,
    pub contract: Option<(String, String)>,
    /// The delegate's outcome judged against the segment system's
    /// active contract; `None` when the segment is not judged (module docs).
    pub judgement: Option<crate::services::run_outcome::Judgement>,
    pub error_cause: Option<&'static str>,
    /// The delegate's release, from the segment's own entering
    /// side (never the caller agent's declaration).
    pub release: crate::services::agent_releases::RunRelease,
    /// The delegate agent that declared `release`; the
    /// segment counts toward that agent's release row.
    pub delegate_agent_id: Option<String>,
}

/// Tool-set signature (the run's own formula, so the two are comparable)
/// and the segment's human-hold time, clipped to its span and restricted to
/// holds its own groups raised. `$1` tenant, `$2` root, `$3`/`$4` span,
/// `$5` groups, `$6` tool names.
const FINISH_SQL: &str = concat!(
    "SELECT (SELECT ",
    tool_set_signature_sql!("t.name"),
    " FROM UNNEST(CAST($6 AS TEXT[])) AS t(name)) AS tool_set_signature, ",
    held_ms_sql!(
        "$1",
        "$2",
        "$3",
        "$4",
        " AND tar.requester_group_id = ANY(CAST($5 AS TEXT[]))"
    ),
    " AS held_ms"
);

/// The segment's elapsed time: from when its earliest call STARTED to its
/// last stamp. Holds are clipped to the same span (`FINISH_SQL` `$3`/`$4`),
/// so a hold is subtracted once and never past the span's edges.
pub fn active_span_ms(d: &SegmentDraft) -> i64 {
    (d.last_action_at - d.active_from).num_milliseconds().max(0)
}

/// Complete the drafts: signature, active time (hold time excluded),
/// the segment system's ACTIVE contract at finalisation, and
/// the delegate's outcome judged against that contract.
///
/// A contract that cannot be read is an error (the run is not finalized and
/// the sweeper retries): judged without it, every delegate outcome would be
/// stamped `undeclared` and the caller's run `delegate_deviated`.
pub async fn finish(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
    drafts: Vec<SegmentDraft>,
    part_releases: &[crate::services::agent_releases::PartRelease],
    run_actor_agent_id: Option<&str>,
) -> Result<Vec<Segment>, sqlx::Error> {
    if drafts.is_empty() {
        return Ok(Vec::new());
    }
    let systems: Vec<String> = drafts.iter().map(|d| d.system_id.clone()).collect();
    let contracts = crate::services::run_outcome::active_contracts(db, tenant_id, &systems).await?;
    let mut out = Vec::with_capacity(drafts.len());
    for d in drafts {
        let (signature, held_ms) = if d.tally.action_count == 0 {
            (None, 0)
        } else {
            let (sig, held): (String, i64) = sqlx::query_as(FINISH_SQL)
                .bind(tenant_id)
                .bind(root_task_id)
                .bind(d.active_from)
                .bind(d.last_action_at)
                .bind(&d.group_ids)
                .bind(&d.tools)
                .fetch_one(db)
                .await?;
            (Some(sig), held)
        };
        let span_ms = active_span_ms(&d);
        let seg_release = crate::services::agent_releases::segment_release(
            &d.group_ids,
            d.caller_agent_id.as_deref(),
            run_actor_agent_id,
            part_releases,
        );
        let contract = contracts.get(&d.system_id);
        let judgement = (d.entered_by_a2a || d.reported_outcome_id.is_some()).then(|| {
            crate::services::run_outcome::judge(
                d.reported_outcome_id.as_deref(),
                contract.and_then(|c| c.catalogue.as_ref()),
            )
        });
        let error_cause = crate::services::run_outcome::segment_error_cause(
            d.outcome,
            d.refused,
            d.entered_by_a2a,
            judgement.as_ref().map(|j| j.conformance),
        );
        out.push(Segment {
            release: seg_release.release,
            delegate_agent_id: seg_release.delegate_agent_id,
            contract: contract.map(|c| (c.id.clone(), c.hash.clone())),
            judgement,
            error_cause,
            active_ms: (span_ms - held_ms).max(0),
            tool_set_signature: signature,
            draft: d,
        });
    }
    Ok(out)
}

const INSERT_SQL: &str = r#"
INSERT INTO ai_system_run_segments (
    id, run_id, tenant_id, root_task_id, root_system_id, segment_system_id,
    min_depth, first_action_at, last_action_at, active_ms,
    action_count, llm_call_count, tool_call_count, error_count, tool_error_count,
    guardrail_block_count, policy_denial_count, approval_request_count,
    total_tokens, cost_usd, models_used, tool_set_signature,
    caller_agent_id, a2a_task_state, outcome, contract_id, contract_hash,
    caller_system_id,
    agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_trust,
    delegate_agent_id,
    outcome_id, outcome_conformance, error_cause, held_approval_request_id
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10,
    $11, $12, $13, $14, $15,
    $16, $17, $18,
    $19, CAST($20 AS NUMERIC) / 100000000, $21, $22,
    $23, $24, $25, $26, $27,
    $28,
    $29, $30, $31, $32,
    $33,
    $34, $35, $36, $37
)
"#;

fn clamp_i32(v: i64) -> i32 {
    i32::try_from(v.max(0)).unwrap_or(i32::MAX)
}

/// Replace a run's segments inside the run's own write transaction.
///
/// Delete-then-insert is what "rewritten on reopen" means: a reopened run
/// may have gained a segment, lost one, or changed every number. A replay
/// run keeps none. The contract stamp is first-write-wins, exactly like the
/// run's: a step arriving after the service promoted a new contract must not
/// retro-stamp the segment that ran under the old one.
#[allow(clippy::too_many_arguments)]
pub async fn replace(
    tx: &mut Transaction<'_, Postgres>,
    run_id: &str,
    tenant_id: &str,
    root_task_id: &str,
    root_system_id: Option<&str>,
    is_replay: bool,
    segments: &[Segment],
) -> Result<usize, sqlx::Error> {
    let previous: Vec<(String, Option<String>, Option<String>)> = sqlx::query_as(
        "DELETE FROM ai_system_run_segments WHERE tenant_id = $1 AND run_id = $2 \
         RETURNING segment_system_id, contract_id, contract_hash",
    )
    .bind(tenant_id)
    .bind(run_id)
    .fetch_all(&mut **tx)
    .await?;
    if is_replay {
        return Ok(0);
    }
    let kept: HashMap<String, (String, String)> = previous
        .into_iter()
        .filter_map(|(sys, id, hash)| Some((sys, (id?, hash?))))
        .collect();

    for s in segments {
        let d = &s.draft;
        let contract = kept.get(&d.system_id).or(s.contract.as_ref());
        sqlx::query(INSERT_SQL)
            .bind(format!("seg-{}", ulid::Ulid::new()))
            .bind(run_id)
            .bind(tenant_id)
            .bind(root_task_id)
            .bind(root_system_id)
            .bind(&d.system_id)
            .bind(i16::try_from(d.min_depth.max(0)).unwrap_or(i16::MAX))
            .bind(d.first_action_at)
            .bind(d.last_action_at)
            .bind(clamp_i32(s.active_ms))
            .bind(clamp_i32(d.tally.action_count))
            .bind(clamp_i32(d.tally.llm_call_count))
            .bind(clamp_i32(d.tally.tool_call_count))
            .bind(clamp_i32(d.tally.error_count))
            .bind(clamp_i32(d.tally.tool_error_count))
            .bind(clamp_i32(d.tally.guardrail_block_count))
            .bind(clamp_i32(d.tally.policy_denial_count))
            .bind(clamp_i32(d.tally.approval_request_count))
            .bind(d.tally.total_tokens)
            .bind(d.tally.cost_units)
            .bind(json!(d.models_used))
            .bind(s.tool_set_signature.as_deref())
            .bind(d.caller_agent_id.as_deref())
            .bind(d.a2a_task_state.as_deref())
            .bind(d.outcome)
            .bind(contract.map(|c| c.0.as_str()))
            .bind(contract.map(|c| c.1.as_str()))
            .bind(d.caller_system_id.as_deref())
            .bind(s.release.name.as_deref())
            .bind(s.release.version.as_deref())
            .bind(s.release.build.as_deref())
            .bind(s.release.trust)
            .bind(s.delegate_agent_id.as_deref())
            .bind(s.judgement.as_ref().and_then(|j| j.outcome_id.as_deref()))
            .bind(s.judgement.as_ref().map(|j| j.conformance))
            .bind(s.error_cause)
            .bind(d.held_approval_request_id.as_deref())
            .execute(&mut **tx)
            .await?;
    }
    Ok(segments.len())
}

#[cfg(test)]
#[path = "run_segments_tests.rs"]
mod tests;
