// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The run ledger.
//!
//! A **run** is the set of `proxy_logs` rows sharing a
//! `(tenant_id, root_task_id)`: the unit of work an AI System performs.
//! This module rolls those steps up into one `ai_system_runs` row.
//!
//! Everything here is aggregation over data the gateway already writes.
//! It adds **no** columns to `proxy_logs` and no work to the request
//! path — deliberately, so nothing about how a request is handled changes.
//! The aggregation runs in SQL rather than by materialising steps in Rust:
//! a long agent run is hundreds of rows, and pulling them into memory to
//! count them would be indefensible when Postgres can do it in one pass.
//!
//! ## Finalization
//!
//! No LLM or MCP protocol has an end-of-run signal, so there are three
//! ways a run gets closed, in precedence order:
//!
//!   1. **Explicit** — the client sent `X-Brutor-Run-End` or called
//!      `POST /v1/runs/{root_task_id}/end`. Cheapest and most accurate.
//!   2. **Terminal** — a step the gateway knows ends the chain.
//!   3. **Idle** — no new step within the AI System's idle window.
//!
//! A step arriving after finalization *reopens* the run: everything is
//! recomputed and `reopened_count` is bumped. Runs reopened more than
//! [`MAX_REOPENS_BEFORE_FRAGMENTED`] times are treated as fragmented and
//! excluded from baselines — a data-quality signal, not an error.
//!
//! ## What is deliberately not derived
//!
//! `exhausted` (the agent hit its max-iteration cap) is **only** set from
//! an explicit client signal. From the proxy we can see that a run stopped;
//! we cannot see whether the framework stopped it because it ran out of
//! iterations or because the client crashed. Guessing would put a number
//! on the "is it looping?" dashboard that nobody could defend, so a run
//! that simply goes quiet is `abandoned` and says so.

use std::collections::{BTreeMap, HashMap};

use chrono::{DateTime, Duration, Utc};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool};

use crate::services::ai_system::{AttributionInputs, nearest_ai_system, resolve_ai_system};
use crate::services::db_errors::DbResultExt;
use crate::services::{agent_releases, run_segments};

// ---------------------------------------------------------------------------
// Wire contract — mirrored in app/database/models/ai_system_run.py
// ---------------------------------------------------------------------------

// What a run PRODUCED is the agent's declared outcome
// (`X-Brutor-Outcome-Id`), judged in `services::run_outcome`. The old
// client-outcome vocabulary (`resolved | escalated | handed_off |
// abandoned_by_user`) and `X-Brutor-Run-Outcome` are gone, not aliased.

/// How a run ended as an EXECUTION. A gateway refusal
/// that ends a run is `errored` with error cause `blocked_by_policy` —
/// `blocked_policy` is no longer a terminal state.
pub mod terminal {
    pub const COMPLETED: &str = "completed";
    pub const COMPLETED_DEGRADED: &str = "completed_degraded";
    pub const EXHAUSTED: &str = "exhausted";
    pub const ERRORED: &str = "errored";
    pub const ABANDONED: &str = "abandoned";
    pub const CANCELLED: &str = "cancelled";

    pub const ALL: [&str; 6] = [
        COMPLETED,
        COMPLETED_DEGRADED,
        EXHAUSTED,
        ERRORED,
        ABANDONED,
        CANCELLED,
    ];
}

pub mod finalized_by {
    pub const EXPLICIT: &str = "explicit";
    pub const TERMINAL: &str = "terminal";
    pub const IDLE: &str = "idle";
    pub const SWEEPER: &str = "sweeper";
    /// An operator stopped this run. Distinct from `idle`
    /// and `sweeper` because the operator's label must win over the
    /// timeout's guess: a run somebody deliberately stopped is not an
    /// abandoned one, and recording it as abandoned loses the only fact
    /// that explains the shape of the evidence.
    pub const OPERATOR: &str = "operator";
}

pub mod chain {
    /// Every step was linked by a chain the gateway itself signed.
    pub const INTACT: &str = "intact";
    /// Complete, but the linkage is the client's word rather than the
    /// gateway's signature — the steps were correlated by `x-brutor-run-id`.
    ///
    /// Separate from [`INTACT`] on purpose. Both mean "we saw the whole run",
    /// but only `intact` means the gateway can prove the steps belong
    /// together. Collapsing them would overstate what the evidence supports,
    /// which is precisely the claim an auditor will press on.
    pub const CLIENT_ASSERTED: &str = "client_asserted";
    /// At least one step arrived through the OTel inlet. Complete as
    /// *reported*: the exporter's trace id grouped the steps and nothing is
    /// known to be missing, but no link was gateway-signed and none was
    /// bound to a credential the gateway issued. Ranked below
    /// [`CLIENT_ASSERTED`] for exactly that reason.
    pub const OBSERVED_EXTERNAL: &str = "observed_external";
    pub const PARTIAL: &str = "partial";
    pub const BROKEN: &str = "broken";

    /// The chain-integrity values that mean **the whole run was observed** —
    /// every step present, correctly grouped. This is the eligibility rule for
    /// anything that computes over complete runs: baselines, drift candidates,
    /// replay suites.
    ///
    /// `client_asserted` belongs here and the distinction matters. The RFC
    /// excludes `partial`/`broken` because *fragments* pollute a baseline —
    /// half a run looks like a short run and manufactures drift. An asserted
    /// run is not a fragment: it is complete and deterministically grouped,
    /// merely linked on the client's word instead of the gateway's signature.
    /// That weaker *provenance* is a disclosure concern (report the split),
    /// not a completeness concern — and excluding it starves every baseline
    /// for exactly the self-driving agents this product targets, since 100%
    /// of their traffic is asserted.
    ///
    /// `observed_external` belongs here too: a whole run, however it was
    /// reported, teaches a baseline what normal looks like. What it cannot
    /// evidence is any control — that is a disclosure, made on the report.
    pub const OBSERVED_COMPLETE: [&str; 3] = [INTACT, CLIENT_ASSERTED, OBSERVED_EXTERNAL];

    /// SQL fragment matching [`OBSERVED_COMPLETE`]. Kept next to the
    /// constant so the two cannot drift apart silently; tests assert every
    /// eligibility query contains this exact fragment.
    pub const OBSERVED_COMPLETE_SQL: &str =
        "chain_integrity IN ('intact', 'client_asserted', 'observed_external')";
}

/// Provenance of a run's steps. Mirrors `RUN_SOURCE_*` in the
/// control plane (`app/database/models/ai_system_run.py`).
pub mod source {
    /// Every step was proxied.
    pub const GATEWAY: &str = "gateway";
    /// Every step was ingested from external telemetry.
    pub const OTEL: &str = "otel";
    /// Some of each — an agent whose model calls ran through the gateway
    /// but whose tool spans arrived by export, say.
    pub const MIXED: &str = "mixed";
    pub const ALL: [&str; 3] = [GATEWAY, OTEL, MIXED];
}

/// Runs reopened more than this by late steps are flagged fragmented.
pub const MAX_REOPENS_BEFORE_FRAGMENTED: i32 = 3;

/// Idle window when the AI System does not override it.
pub const DEFAULT_RUN_IDLE_TIMEOUT_SECONDS: i64 = 120;

/// How far back the sweeper looks for candidates. Bounds the scan: a run
/// whose last step is older than this and which was never finalized is not
/// worth chasing, and letting the query walk the whole of `proxy_logs`
/// would turn a background tick into a table scan.
pub const SWEEP_LOOKBACK_SECONDS: i64 = 24 * 3600;

/// Proxy types that count as tool calls (see `constants::proxy`).
const TOOL_PROXY_TYPES: [&str; 2] = ["mcp", "virtual-mcp"];

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

/// Raw rollup of one run's steps, straight out of Postgres.
#[derive(Debug, Clone, FromRow)]
pub struct RunAggregate {
    pub tenant_id: String,
    pub root_task_id: String,
    pub started_at: DateTime<Utc>,
    pub ended_at: DateTime<Utc>,
    /// Elapsed wall clock, first step to last. Includes any time the run
    /// sat parked on a human approval — see [`Self::active_duration_ms`].
    pub duration_ms: i64,
    /// Time inside `[started_at, ended_at]` covered by the run's approval
    /// holds (merged, so concurrent holds are not double-counted). See
    /// [`held_ms_sql`].
    pub awaiting_human_ms: i64,
    /// `duration_ms − awaiting_human_ms`: the time the AGENT spent executing.
    /// This, not the elapsed figure, is what a per-run duration ceiling and
    /// the duration drift baseline measure — a decision approved thirteen
    /// hours later is governance working, not a thirteen-hour runtime.
    pub active_duration_ms: i64,
    /// Σ [`Self::parts`] — see [`aggregate_run`]. Never read from SQL.
    #[sqlx(skip)]
    pub action_count: i64,
    /// DISTINCT client-asserted phases; 0 = none asserted.
    pub step_count: i64,
    pub llm_call_count: i64,
    pub tool_call_count: i64,
    pub skill_call_count: i64,
    pub a2a_call_count: i64,
    /// Passes of the agent loop. Read [`Self::turn_attribution`] before
    /// treating this as something the client reported.
    pub turn_count: i64,
    /// `client_asserted` | `inferred` — where `turn_count` came from.
    pub turn_attribution: String,
    pub max_delegation_depth: i32,
    pub distinct_tools: i64,
    pub tool_signature: Option<String>,
    pub tool_set_signature: Option<String>,
    /// Sorted names of the tools the run called or tried to call.
    pub tools_used: Value,
    /// Σ [`Self::parts`].
    #[sqlx(skip)]
    pub total_tokens: i64,
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    /// Σ [`Self::parts`], in USD. [`Self::total_cost_units`] is the exact
    /// figure the run row stores.
    #[sqlx(skip)]
    pub total_cost_usd: f64,
    /// Σ [`Self::parts`] in `run_segments::COST_UNITS_PER_USD` units.
    #[sqlx(skip)]
    pub total_cost_units: i64,
    pub cache_hit_count: i64,
    pub tokens_saved: i64,
    pub error_count: i64,
    pub fallback_count: i64,
    pub tool_error_count: i64,
    pub guardrail_trip_count: i64,
    pub guardrail_block_count: i64,
    pub policy_denial_count: i64,
    pub approval_request_count: i64,
    pub input_bytes: i64,
    pub output_bytes: i64,
    pub first_prompt_tokens: Option<i32>,
    /// The largest context any one call in the run sent its model, cached
    /// tokens included. `None` = no call reported usage (not measured).
    pub peak_context_tokens: Option<i32>,
    /// The largest share of a model's context window any call used. `None`
    /// when no call's model has a known window, never 0.
    pub peak_context_utilization: Option<f64>,
    /// The peak over the first measured context of the SAME agent: how much
    /// that agent accumulated. Its own first call, because in a multi-agent
    /// run the run's first call may be someone else's.
    pub context_growth: Option<f64>,
    pub rag_used: bool,
    pub rag_chunks: i64,
    pub prompt_fingerprint: Option<String>,
    pub rag_collection_ids: Option<String>,
    pub trace_root_kind: Option<String>,
    pub client_label: Option<String>,
    /// Distinct resource groups the steps were authorized against — the
    /// input to root-plus-`via` attribution.
    pub group_ids: Vec<String>,
    pub actor_agent_id: Option<String>,
    pub api_key_id: Option<String>,
    pub gateway_user_id: Option<String>,
    pub subject_id: Option<String>,
    pub subject_kind: Option<String>,
    // Last-step facts, for terminal-state derivation.
    pub last_step_id: String,
    pub last_success: bool,
    pub last_guardrail_blocked: bool,
    pub last_policy_denied: bool,
    // Chain integrity inputs.
    pub any_sig_failed: bool,
    pub any_client_dropped: bool,
    pub any_client_asserted: bool,
    // Provenance inputs.
    pub any_external: bool,
    pub any_gateway: bool,
    // The terminal state a client asked for via `X-Brutor-Run-End` on
    // one of this run's steps, if any. Read off the row rather than passed in,
    // so a run whose immediate finalization was missed (writer restart, a
    // transient DB error) is still closed as `explicit` with the state the
    // client asked for whenever the sweeper next reaches it.
    pub client_run_end: Option<String>,
    /// The run's failing call: its LAST unsuccessful action (a
    /// call held for a human is not unsuccessful), as
    /// `run_outcome::CallFacts` JSON. Read for the error cause.
    pub failing_call: Option<Value>,
    // Sub-aggregations.
    pub models_used: Option<Value>,
    pub tool_error_types: Option<Value>,
    /// The run's steps partitioned by resource group, read in
    /// the same snapshot as everything above. The conserved totals are their
    /// sum, and the segments are built from them.
    #[sqlx(skip)]
    pub parts: Vec<run_segments::GroupPart>,
    /// Steps that parented work in another group — candidate entering calls.
    #[sqlx(skip)]
    pub delegation_calls: Vec<run_segments::DelegationCall>,
    /// The run's release (first carrying action of the actor).
    #[sqlx(skip)]
    pub release: agent_releases::RunRelease,
    /// What the run declared and which records it touched.
    #[sqlx(skip)]
    pub scope: crate::services::data_scope::RunScopeFacts,
    /// `sha256:…` over the actor's first software surface.
    #[sqlx(skip)]
    pub implementation_fingerprint: Option<String>,
    /// First carrying action per (group, actor), for segments.
    #[sqlx(skip)]
    pub part_releases: Vec<agent_releases::PartRelease>,
}

/// Time a run spent parked on a human, as a scalar SQL subquery.
///
/// A call held for approval (HTTP 202, `tool_approval_requests` row) is
/// retried by the agent on the SAME run once a person decides — possibly
/// hours later — so the run's elapsed wall clock includes the human's wait.
/// This measures that wait so it can be taken back out.
///
/// Each hold is the interval `created_at → end`, where end is:
///
/// * `expires_at` for an `expired` hold (it lapsed; nobody answered after that),
/// * `resolved_at` once a person approved or rejected it (`consumed` keeps the
///   approval's `resolved_at` — spending the token is the agent's act, not the
///   person's),
/// * otherwise — still pending — `now`, but never past `expires_at`.
///
/// Intervals are clipped to `[$from, $to]` and **merged** before summing
/// (gaps-and-islands), so two holds open at once are one wait, not two. The
/// result is therefore never more than `$to − $from`.
///
/// A macro rather than a `const` so the ledger aggregate (which clips to the
/// run's own first/last step) and the live run clock (which clips to the
/// Redis `t0` and now) share ONE definition. The control plane's
/// backfill restates it in SQL; `tests/integration/run_ledger.rs` pins both
/// against the same fixtures.
macro_rules! held_ms_sql {
    ($tenant:literal, $root:literal, $from:literal, $to:literal) => {
        held_ms_sql!($tenant, $root, $from, $to, "")
    };
    // `$extra` narrows which holds count (a segment counts only the holds its
    // own groups raised); it is appended to the hold filter verbatim.
    ($tenant:literal, $root:literal, $from:literal, $to:literal, $extra:literal) => {
        concat!(
            // SUM(bigint) is NUMERIC; cast back so both callers decode i64.
            "(SELECT CAST(COALESCE(SUM(CAST(EXTRACT(EPOCH FROM (isl.e - isl.s)) * 1000 AS BIGINT)), 0) AS BIGINT)
   FROM (SELECT MIN(o.s) AS s, MAX(o.e) AS e
           FROM (SELECT r.s, r.e,
                        SUM(CASE WHEN r.reach IS NULL OR r.s > r.reach THEN 1 ELSE 0 END)
                            OVER (ORDER BY r.s, r.e ROWS UNBOUNDED PRECEDING) AS island
                   FROM (SELECT c.s, c.e,
                                MAX(c.e) OVER (ORDER BY c.s, c.e
                                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS reach
                           FROM (SELECT GREATEST(tar.created_at, ", $from, ") AS s,
                                        LEAST(CASE
                                                  WHEN tar.status = 'expired'
                                                      THEN LEAST(COALESCE(tar.resolved_at, tar.expires_at), tar.expires_at)
                                                  WHEN tar.resolved_at IS NOT NULL
                                                      THEN tar.resolved_at
                                                  ELSE LEAST(tar.expires_at, NOW())
                                              END, ", $to, ") AS e
                                   FROM tool_approval_requests tar
                                  WHERE tar.tenant_id = ", $tenant, "
                                    AND tar.originating_trace_root_id = ", $root, $extra, ") c
                          WHERE c.e > c.s) r) o
          GROUP BY o.island) isl)"
        )
    };
}
pub(crate) use held_ms_sql;

/// Merged human-hold time for one run between two instants — the live run
/// clock's view of [`held_ms_sql`]. `$1` tenant, `$2` root_task_id, `$3`
/// from, `$4` to.
const HELD_MS_SQL: &str = concat!(
    "SELECT ",
    held_ms_sql!("$1", "$2", "$3", "$4"),
    " AS held_ms"
);

/// How much of `[from, to]` this run spent parked on a human approval.
///
/// The source of truth is `tool_approval_requests`, the same rows the ledger
/// aggregate reads, so the live gate and the finalized run agree on what
/// counted as a hold.
pub async fn held_ms_between(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
) -> Result<i64, sqlx::Error> {
    if to <= from {
        return Ok(0);
    }
    sqlx::query_scalar::<_, i64>(HELD_MS_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(from)
        .bind(to)
        .fetch_one(db)
        .await
}

/// The per-step counts a run and its segments share.
///
/// ONE definition, spliced into both [`AGGREGATE_SQL`] and
/// `run_segments::PARTS_SQL`, so a segment's `error_count` means exactly
/// what its run's does. Expects `pl` = `proxy_logs` and `$3` = the tool proxy
/// types; ends with a trailing comma.
macro_rules! step_tally_sql {
    () => {
        r#"
    COUNT(*) FILTER (WHERE pl.proxy_type = 'llm')              AS llm_call_count,
    -- A skill run arrives on two surfaces: the deprecated REST route writes
    -- proxy_type 'skill', the progressive-disclosure path (the one agents
    -- are told to use) writes an 'mcp' row on the skills system server with
    -- proxy_subtype 'skill_run'. Both are skill calls, and neither is a tool
    -- call: counting the MCP form as a tool made skill_call_count read 0 for
    -- every system built the documented way .
    COUNT(*) FILTER (WHERE pl.proxy_type = ANY($3)
                       AND COALESCE(pl.proxy_subtype, '') <> 'skill_run')
                                                               AS tool_call_count,
    -- A call held for a human (proxy_subtype 'approval_required', HTTP 202)
    -- is logged with success = false, but it is governance working, not a
    -- failure: it is counted once, as an approval request, below. Counting it
    -- as an error as well made every held decision a "tool error", which
    -- dragged the reliability signal down and raised tool_error_rate drift on
    -- a system whose tools never failed .
    COUNT(*) FILTER (WHERE NOT pl.success
                       AND pl.proxy_subtype IS DISTINCT FROM 'approval_required')
                                                               AS error_count,
    COUNT(*) FILTER (WHERE NOT pl.success AND pl.proxy_type = ANY($3)
                       AND pl.proxy_subtype IS DISTINCT FROM 'approval_required')
                                                               AS tool_error_count,
    COUNT(*) FILTER (WHERE pl.guardrail_blocked)               AS guardrail_block_count,
    COUNT(*) FILTER (WHERE pl.argument_policy_denied)          AS policy_denial_count,
    COUNT(*) FILTER (WHERE pl.proxy_subtype = 'approval_required')
                                                               AS approval_request_count,
"#
    };
}
pub(crate) use step_tally_sql;

/// When a step STARTED. A `proxy_logs` row is stamped when its call
/// FINISHES (`proxy_logger` sets `timestamp` as it writes the row), so the
/// step ran over `[timestamp - duration_ms, timestamp]`. Measuring a run or
/// segment between stamps read 0 ms for every one-call run and segment and
/// cut the first call out of every longer one.
///
/// Shared by the run (`started_at`) and its segments (`active_from`) so the
/// two clocks agree. A duration that cannot be a real call counts as 0 —
/// exactly the stamp the old measure used, never an error that would leave
/// the run unfinalized: NULL; NaN (Postgres sorts it above every number,
/// Infinity included); non-positive; and anything from 2^31 ms (~24.8
/// days) up, which no `integer` duration column could hold and whose
/// interval product could overflow.
macro_rules! step_started_at_sql {
    () => {
        "(pl.timestamp - CASE WHEN pl.duration_ms > 0 AND pl.duration_ms < 2147483648 \
         THEN pl.duration_ms ELSE 0 END * INTERVAL '1 millisecond')"
    };
}
pub(crate) use step_started_at_sql;

/// Unordered tool-set signature over `$col`. Shared by the run and its
/// segments so the two are comparable (the ordering is the database
/// collation's, which is why this is SQL and not Rust).
macro_rules! tool_set_signature_sql {
    ($col:literal) => {
        concat!(
            "MD5(COALESCE(STRING_AGG(DISTINCT ",
            $col,
            ", ',' ORDER BY ",
            $col,
            "), ''))"
        )
    };
}
pub(crate) use tool_set_signature_sql;

/// The SQL that turns steps into a run. One statement, one pass over the
/// steps, plus one bounded lookup of the run's approval holds.
///
/// `$1` tenant_id, `$2` root_task_id.
pub(crate) const AGGREGATE_SQL: &str = concat!(
    r#"
WITH run AS (
SELECT
    pl.tenant_id,
    pl.root_task_id,
    -- Started when the first call STARTED; ended at the last stamp (rows are
    -- stamped on completion — see `step_started_at_sql!`). `ended_at` stays
    -- the last stamp: the idle sweeper compares it with new stamps.
    MIN("#,
    step_started_at_sql!(),
    r#")                                                       AS started_at,
    MAX(pl.timestamp)                                          AS ended_at,
    GREATEST(0, CAST(EXTRACT(EPOCH FROM (MAX(pl.timestamp) - MIN("#,
    step_started_at_sql!(),
    r#"))) * 1000 AS BIGINT))
                                                               AS duration_ms,
    -- action_count, total_tokens and total_cost_usd are NOT computed here:
    -- they are the sum of the per-group parts (`run_segments::PARTS_SQL`),
    -- read in the same snapshot, so a run always equals its root's own share
    -- plus its segments. See `aggregate_run`.
"#,
    step_tally_sql!(),
    r#"
    COUNT(*) FILTER (WHERE pl.proxy_type = 'skill'
                        OR COALESCE(pl.proxy_subtype, '') = 'skill_run')
                                                               AS skill_call_count,
    COUNT(*) FILTER (WHERE pl.proxy_type = 'a2a')              AS a2a_call_count,

    -- Turns. A turn is one pass of the agent loop: a model call plus
    -- the calls it triggered. When the client asserted turn ids we count
    -- those; otherwise we infer, because a model call is what opens a loop
    -- iteration. Embeddings are excluded — a retrieval is work *inside* a
    -- turn, not a turn of its own.
    --
    -- The two are reported separately from `turn_attribution` below rather
    -- than blended, so an inferred number is never mistaken for a reported
    -- one.
    CASE
        WHEN COUNT(pl.turn_id) > 0 THEN COUNT(DISTINCT pl.turn_id)
        ELSE COUNT(*) FILTER (
            WHERE pl.proxy_type = 'llm'
              AND COALESCE(pl.proxy_subtype, '') <> 'embeddings'
        )
    END                                                        AS turn_count,
    CASE
        WHEN COUNT(pl.turn_id) > 0 THEN 'client_asserted'
        ELSE 'inferred'
    END                                                        AS turn_attribution,

    -- Steps. A step is a logical phase of the plan, and unlike a
    -- turn it is NOT inferable from traffic: nothing here distinguishes
    -- "still researching" from "now drafting". So this counts what the
    -- client asserted and nothing else — 0 means none were asserted, which
    -- is a different statement from "the run had one phase".
    COUNT(DISTINCT pl.step_id)                                 AS step_count,
    COALESCE(MAX(pl.delegation_depth), 0)                      AS max_delegation_depth,
    COUNT(DISTINCT pl.mcp_capability_name)                     AS distinct_tools,
    -- The names behind that count, attempted calls included, sorted. A call
    -- the gateway refused is still something the run tried to do.
    COALESCE(JSONB_AGG(DISTINCT pl.mcp_capability_name)
        FILTER (WHERE pl.mcp_capability_name IS NOT NULL), '[]'::jsonb)
                                                               AS tools_used,

    -- Ordered tool sequence: trajectory *shape*. `id` breaks ties so two
    -- steps with the same millisecond timestamp can't hash differently
    -- between runs of the same query.
    MD5(COALESCE(
        STRING_AGG(pl.mcp_capability_name, '>' ORDER BY pl.timestamp, pl.id)
            FILTER (WHERE pl.mcp_capability_name IS NOT NULL),
        ''))                                                   AS tool_signature,
    -- Unordered tool set. Comparing the two separates "it uses different
    -- tools" from "it uses the same tools in a different order".
    "#,
    tool_set_signature_sql!("pl.mcp_capability_name"),
    r#"                                                   AS tool_set_signature,

    COALESCE(SUM(pl.llm_prompt_tokens), 0)                     AS prompt_tokens,
    COALESCE(SUM(pl.llm_completion_tokens), 0)                 AS completion_tokens,
    COUNT(*) FILTER (WHERE pl.llm_cache_hit)                   AS cache_hit_count,
    COALESCE(SUM(pl.llm_tokens_saved), 0)                      AS tokens_saved,

    COUNT(*) FILTER (WHERE pl.llm_used_fallback)               AS fallback_count,

    -- A trip is any guardrail engagement; a block (counted in the shared
    -- tally) is one that stopped the call. Kept apart because a high trip
    -- rate with no blocks is a tuning signal, while blocks are a governance
    -- outcome.
    COUNT(*) FILTER (WHERE pl.guardrail_blocked OR pl.pii_detected)
                                                               AS guardrail_trip_count,

    COALESCE(SUM(pl.request_size_bytes), 0)                    AS input_bytes,
    COALESCE(SUM(pl.response_size_bytes), 0)                   AS output_bytes,
    -- Prompt size of the *first* LLM step: the task as posed, before the
    -- system's own accumulated context. This is the input-drift signal
    -- that separates "the system changed" from "the questions changed".
    -- `llm_context_tokens`, so a cached system prompt still counts on every
    -- provider, and only a call that read a context: an embedding or a
    -- response-cache hit is not the task reaching a model.
    (ARRAY_AGG(pl.llm_context_tokens ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.llm_context_tokens IS NOT NULL))[1]   AS first_prompt_tokens,

    -- Context size. A peak, not the last call: an agent that compacts shrinks
    -- before it finishes, which would hide exactly the risk this measures.
    -- Never summed across agents: two agents' contexts are two contexts.
    MAX(pl.llm_context_tokens)                                 AS peak_context_tokens,
    (SELECT MAX(pl2.llm_context_tokens::float8 / w.context_window)
       FROM proxy_logs pl2
       CROSS JOIN LATERAL (
            SELECT MIN(c.context_window) AS context_window
              FROM llm_catalog c
             WHERE c.context_window > 0
               AND ((c.provider_key = pl2.llm_provider AND c.model_name = pl2.llm_model_used)
                    OR c.id IN (SELECT m.catalog_id FROM llm_model m
                                 WHERE m.tenant_id = $1 AND m.model_name = pl2.llm_model_used))
       ) w
      WHERE pl2.tenant_id = $1 AND pl2.root_task_id = $2
        AND pl2.llm_context_tokens > 0)                        AS peak_context_utilization,
    (WITH measured AS (
          SELECT COALESCE(pl2.actor_agent_id, pl2.resource_group_id, '') AS who,
                 pl2.llm_context_tokens AS tokens, pl2.timestamp AS at, pl2.id AS id
            FROM proxy_logs pl2
           WHERE pl2.tenant_id = $1 AND pl2.root_task_id = $2
             AND pl2.llm_context_tokens > 0),
          peak AS (SELECT who, tokens FROM measured ORDER BY tokens DESC, at, id LIMIT 1),
          opening AS (SELECT m.tokens FROM measured m JOIN peak p ON m.who = p.who
                       ORDER BY m.at, m.id LIMIT 1)
     SELECT p.tokens::float8 / o.tokens FROM peak p, opening o) AS context_growth,

    COALESCE(BOOL_OR(pl.rag_enabled), false)                   AS rag_used,
    COALESCE(SUM(pl.rag_chunks_retrieved), 0)                  AS rag_chunks,
    -- What the run was told, and what it read. The dominant
    -- instruction fingerprint across the run's model calls, and the first
    -- collection set it retrieved from: the two doors that open with no
    -- release, now recorded so the drift cause ladder can name them.
    MODE() WITHIN GROUP (ORDER BY pl.prompt_fingerprint)
        FILTER (WHERE pl.prompt_fingerprint IS NOT NULL)       AS prompt_fingerprint,
    (ARRAY_AGG(pl.rag_collection_ids ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.rag_collection_ids IS NOT NULL))[1]   AS rag_collection_ids,

    (ARRAY_AGG(pl.trace_root_kind ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.trace_root_kind IS NOT NULL))[1]      AS trace_root_kind,
    -- The normalised label (`X-Brutor-Client`, else
    -- the User-Agent's product name), not the raw User-Agent: a client is
    -- the channel a request came through, not its HTTP library version.
    (ARRAY_AGG(pl.client_label ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.client_label IS NOT NULL))[1]         AS client_label,

    -- Ordered by when the group was first seen, NOT DISTINCT.
    --
    -- `ARRAY_AGG(DISTINCT x)` sorts, and the consumer takes `.first()` as the
    -- run's owning group. So a run was attributed to whichever of its groups
    -- sorted alphabetically first, not to the one that started it: a run
    -- rooted in `zeta-support` that called a shared `alpha-search` service was
    -- filed under `alpha-search`, and every cost, health and paging decision
    -- that follows attribution went to the wrong owner. Duplicates are removed
    -- in Rust, preserving first-appearance order, so `[0]` is the root step's
    -- group.
    COALESCE(ARRAY_AGG(pl.resource_group_id ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.resource_group_id IS NOT NULL), '{}') AS group_ids,

    (ARRAY_AGG(pl.actor_agent_id ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.actor_agent_id IS NOT NULL))[1]       AS actor_agent_id,
    (ARRAY_AGG(pl.api_key_id ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.api_key_id IS NOT NULL))[1]           AS api_key_id,
    (ARRAY_AGG(pl.gateway_user_id ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.gateway_user_id IS NOT NULL))[1]      AS gateway_user_id,
    -- The human (or service) an agent acted FOR. Stamped per step from
    -- the signed delegation chain / api-key user; carried onto the run so
    -- "which person did this agent act for" is a run-level fact and a
    -- Tier-A check vocabulary, not something recovered from proxy_logs.
    (ARRAY_AGG(pl.subject_id ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.subject_id IS NOT NULL))[1]           AS subject_id,
    (ARRAY_AGG(pl.subject_kind ORDER BY pl.timestamp, pl.id)
        FILTER (WHERE pl.subject_kind IS NOT NULL))[1]         AS subject_kind,

    (ARRAY_AGG(pl.id ORDER BY pl.timestamp DESC, pl.id DESC))[1] AS last_step_id,
    COALESCE((ARRAY_AGG(pl.success ORDER BY pl.timestamp DESC, pl.id DESC))[1], true)
                                                               AS last_success,
    COALESCE((ARRAY_AGG(pl.guardrail_blocked ORDER BY pl.timestamp DESC, pl.id DESC))[1], false)
                                                               AS last_guardrail_blocked,
    COALESCE((ARRAY_AGG(pl.argument_policy_denied ORDER BY pl.timestamp DESC, pl.id DESC))[1], false)
                                                               AS last_policy_denied,

    COALESCE(BOOL_OR(pl.trace_continuity = 'sig_failed'), false)     AS any_sig_failed,
    COALESCE(BOOL_OR(pl.trace_continuity = 'client_dropped'), false) AS any_client_dropped,
    COALESCE(BOOL_OR(pl.trace_continuity = 'client_asserted'), false) AS any_client_asserted,
    COALESCE(BOOL_OR(pl.source = 'otel'), false)                      AS any_external,
    COALESCE(BOOL_OR(pl.source IS DISTINCT FROM 'otel'), false)       AS any_gateway,

    -- The client's explicit end marker. MAX() rather than "the last
    -- step's value" on purpose: the marker is set on one step and NULL on the
    -- rest, so MAX picks it out wherever it landed. If a client somehow marks
    -- twice with different states, MAX is at least deterministic — and the
    -- immediate-finalize path already dedupes per run, so that needs a client
    -- contradicting itself across batches to occur at all.
    MAX(pl.run_end_state)                                      AS client_run_end,

    -- The failing call, for the error cause: the LAST step that
    -- did not succeed. A call held for a human (approval_required) is
    -- governance working, not a failure — the same rule as error_count.
    (ARRAY_AGG(JSONB_BUILD_OBJECT(
                'id',                     pl.id,
                'at',                     pl.timestamp,
                'proxy_type',             pl.proxy_type,
                'proxy_subtype',          pl.proxy_subtype,
                'http_status_code',       pl.http_status_code,
                'guardrail_blocked',      pl.guardrail_blocked,
                'argument_policy_denied', pl.argument_policy_denied)
            ORDER BY pl.timestamp DESC, pl.id DESC)
        FILTER (WHERE NOT pl.success
                  AND pl.proxy_subtype IS DISTINCT FROM 'approval_required'))[1]
                                                               AS failing_call,

    -- Per-model outcome attribution.
    --
    -- `calls` alone answers "which models served this run" but not "which one
    -- went wrong". When the drift cause ladder reaches its `model_change`
    -- rung it can currently say the model mix moved and nothing more — an
    -- operator then has to open individual runs to find out which model
    -- degraded. These counters make that a query.
    --
    -- Aggregated from hops the gateway already writes, so this costs one
    -- extra pass over rows that are already in the CTE's working set and adds
    -- nothing to the request path. Keys are additive: older rows carry
    -- `{model, calls}` only, and every reader must tolerate their absence.
    (SELECT JSONB_AGG(JSONB_BUILD_OBJECT(
                'model',   m.model,
                'calls',   m.calls,
                'errors',  m.errors,
                'blocked', m.blocked,
                'tokens',  m.tokens,
                'cost',    m.cost,
                'p50_ms',  m.p50_ms))
       FROM (SELECT pl2.llm_model_used                          AS model,
                    COUNT(*)                                    AS calls,
                    COUNT(*) FILTER (WHERE NOT pl2.success)     AS errors,
                    COUNT(*) FILTER (WHERE pl2.guardrail_blocked) AS blocked,
                    COALESCE(SUM(pl2.llm_total_tokens), 0)      AS tokens,
                    COALESCE(SUM(pl2.llm_estimated_cost_usd), 0.0) AS cost,
                    -- Median rather than mean: one cold start or one retried
                    -- hop otherwise dominates a run with few calls per model.
                    -- The per-hop column is `duration_ms` (double precision);
                    -- `response_time_ms` is the usage-ledger column and does
                    -- not exist here.
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY pl2.duration_ms)               AS p50_ms
               FROM proxy_logs pl2
              WHERE pl2.tenant_id = $1 AND pl2.root_task_id = $2
                AND pl2.llm_model_used IS NOT NULL
              GROUP BY pl2.llm_model_used) m)                  AS models_used,

    (SELECT JSONB_OBJECT_AGG(e.et, e.c)
       FROM (SELECT error_type AS et, COUNT(*) AS c
               FROM proxy_logs
              WHERE tenant_id = $1 AND root_task_id = $2
                AND proxy_type = ANY($3) AND NOT success
                AND proxy_subtype IS DISTINCT FROM 'approval_required'
                AND error_type IS NOT NULL
              GROUP BY error_type) e)                          AS tool_error_types

FROM proxy_logs pl
WHERE pl.tenant_id = $1 AND pl.root_task_id = $2
GROUP BY pl.tenant_id, pl.root_task_id
)
SELECT run.*,
       held.awaiting_human_ms,
       GREATEST(0, run.duration_ms - held.awaiting_human_ms) AS active_duration_ms
  FROM run
 CROSS JOIN LATERAL (SELECT "#,
    // Clipped to the run's own first and last step: a hold still pending
    // when the run is aggregated contributes only up to its last step,
    // because elapsed time stops there too.
    held_ms_sql!("$1", "$2", "run.started_at", "run.ended_at"),
    r#" AS awaiting_human_ms) held
"#
);

/// Load the rollup for one run. `None` when the run has no steps.
///
/// Three statements — the rollup, the per-group parts, the delegation calls —
/// in ONE read-only REPEATABLE READ transaction, so they see the same steps
/// even while the writer is appending more. That shared snapshot is what lets
/// the run's conserved totals be *defined* as the sum of the parts:
/// a step that landed between two READ COMMITTED statements
/// would otherwise be in one and not the other. Read-only snapshots never
/// raise serialization failures, so this adds no retry path.
pub async fn aggregate_run(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<Option<RunAggregate>, sqlx::Error> {
    let mut tx = db.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(&mut *tx)
        .await?;
    let mut agg = sqlx::query_as::<_, RunAggregate>(AGGREGATE_SQL)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(&TOOL_PROXY_TYPES[..])
        .fetch_optional(&mut *tx)
        .await?;
    if let Some(a) = agg.as_mut() {
        // Dedupe preserving first-appearance order, so
        // `group_ids[0]` is the group the run STARTED in. The SQL aggregates
        // in step order rather than `DISTINCT` (which sorts) precisely so
        // this is possible; see the comment on the column.
        let mut seen = std::collections::HashSet::new();
        a.group_ids.retain(|g| seen.insert(g.clone()));

        a.parts = run_segments::load_parts(&mut tx, tenant_id, root_task_id, &TOOL_PROXY_TYPES[..])
            .await?;
        a.delegation_calls = run_segments::load_calls(&mut tx, tenant_id, root_task_id).await?;
        // Same snapshot: the run's release is the actor's, and
        // each segment's is its delegate's.
        let (release, fingerprint) = agent_releases::load_run_release(
            &mut tx,
            tenant_id,
            root_task_id,
            a.actor_agent_id.as_deref(),
        )
        .await?;
        a.release = release;
        a.implementation_fingerprint = fingerprint;
        a.scope =
            crate::services::data_scope::load_run_facts(&mut tx, tenant_id, root_task_id).await?;
        a.part_releases =
            agent_releases::load_part_releases(&mut tx, tenant_id, root_task_id).await?;
        let totals = run_segments::Tally::of_parts(&a.parts);
        a.action_count = totals.action_count;
        a.total_tokens = totals.total_tokens;
        a.total_cost_units = totals.cost_units;
        a.total_cost_usd = run_segments::units_to_usd(totals.cost_units);
    }
    tx.commit().await?;
    Ok(agg)
}

// ---------------------------------------------------------------------------
// Terminal state + chain integrity — pure, so they can be tested properly
// ---------------------------------------------------------------------------

/// The facts terminal-state derivation depends on. Extracted from the
/// aggregate so the decision itself is a pure function: this is the piece
/// most likely to be argued about, and it should be arguable against a
/// table of cases rather than against a database.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TerminalInputs {
    pub last_success: bool,
    pub last_guardrail_blocked: bool,
    pub last_policy_denied: bool,
    pub fallback_count: i64,
    pub error_count: i64,
    pub closed_by_idle: bool,
}

/// Derive a run's outcome.
///
/// `explicit` short-circuits everything: a client that tells us how its
/// run ended knows better than we can infer.
/// Reconcile a caller-supplied terminal state with a marker the client left on
/// one of the run's steps via `X-Brutor-Run-End`.
///
/// Returns `(explicit_state_to_use, finalized_by_to_record)`.
///
/// Precedence, and why:
///
/// * **A caller-supplied state wins.** Its only caller is
///   `POST /v1/runs/{id}/end`, where someone is stating the outcome *now*.
///   A marker on a step is older information by construction.
/// * **Otherwise the step marker is used, and the close is recorded as
///   `explicit`.** This is what makes the header durable rather than
///   best-effort: if the post-commit finalize never ran — writer restart, a
///   transient DB error — the idle sweeper still finds the marker and closes
///   the run the way the client asked, rather than filing a run that finished
///   cleanly as `abandoned`/`idle`.
/// * **With neither, nothing changes.** Derivation from the steps, closed by
///   whatever mechanism got here.
///
/// Kept separate from [`derive_terminal_state`] because it decides *whose
/// account of the run we believe*, which is a different question from *what
/// the steps imply* — and because `finalize_run` needs a live database, so
/// folding this into it would leave the precedence rule untestable.
fn resolve_explicit_close<'a>(
    closed_by: &'a str,
    caller_state: Option<&'a str>,
    step_marker: Option<&'a str>,
) -> (Option<&'a str>, &'a str) {
    match (caller_state, step_marker) {
        (Some(s), _) => (Some(s), closed_by),
        (None, Some(m)) => (Some(m), finalized_by::EXPLICIT),
        (None, None) => (None, closed_by),
    }
}

pub fn derive_terminal_state(inputs: TerminalInputs, explicit: Option<&str>) -> &'static str {
    if let Some(state) = explicit {
        if let Some(known) = terminal::ALL.iter().find(|s| **s == state) {
            return known;
        }
        // An unrecognised client-supplied state is ignored rather than
        // stored: `terminal_state` feeds every downstream aggregate, and
        // one typo'd value would create a phantom category on the
        // terminal-state mix chart that nobody could explain.
    }

    // A block on the *last* step ended the run: the system could
    // not do its job, so the run is `errored` (its error cause,
    // `blocked_by_policy`, says why; see `run_outcome::derive_error_cause`).
    // A block mid-run that the system recovered from is a guardrail trip,
    // not the run's end — the run completes (degraded) as before.
    if inputs.last_guardrail_blocked || inputs.last_policy_denied || !inputs.last_success {
        return terminal::ERRORED;
    }

    // Nothing said the run was over and it simply stopped emitting. This
    // is where a max-iteration exhaustion *would* land if the client did
    // not report it — see the module docs on why we do not guess.
    if inputs.closed_by_idle {
        return terminal::ABANDONED;
    }

    if inputs.fallback_count > 0 || inputs.error_count > 0 {
        return terminal::COMPLETED_DEGRADED;
    }

    terminal::COMPLETED
}

/// Classify how much of the run we actually observed, and on whose word.
pub fn derive_chain_integrity(
    any_sig_failed: bool,
    any_client_dropped: bool,
    any_client_asserted: bool,
    any_external: bool,
) -> &'static str {
    if any_sig_failed {
        // A forged or corrupted link is worse than a missing one: we know
        // the chain was tampered with, not merely truncated.
        chain::BROKEN
    } else if any_client_dropped {
        chain::PARTIAL
    } else if any_external {
        // Complete as reported, and nothing about it was signed or bound to
        // a credential. Outranks an assertion: the weakest link names the
        // chain.
        chain::OBSERVED_EXTERNAL
    } else if any_client_asserted {
        // Nothing is missing, but at least one link is the client's
        // correlation rather than a gateway signature. Reported separately so
        // "we watched the whole run" is never confused with "we can prove the
        // steps belong together".
        chain::CLIENT_ASSERTED
    } else {
        chain::INTACT
    }
}

/// Where the run's steps came from. See [`source`].
pub fn derive_run_source(any_external: bool, any_gateway: bool) -> &'static str {
    match (any_external, any_gateway) {
        (true, true) => source::MIXED,
        (true, false) => source::OTEL,
        _ => source::GATEWAY,
    }
}

/// Which actor the run is attributed to. Order matters: an agent identity
/// is more specific than the human or key that provisioned it.
pub fn derive_actor(agg: &RunAggregate) -> (Option<&'static str>, Option<String>) {
    if let Some(agent) = agg.actor_agent_id.clone() {
        return (Some("agent_identity"), Some(agent));
    }
    if let Some(user) = agg.gateway_user_id.clone() {
        return (Some("end_user"), Some(user));
    }
    if let Some(key) = agg.api_key_id.clone() {
        return (Some("api_key"), Some(key));
    }
    if agg.trace_root_kind.as_deref() == Some("scheduled_job") {
        return (Some("scheduler"), None);
    }
    (None, None)
}

// ---------------------------------------------------------------------------
// Finalization
// ---------------------------------------------------------------------------

/// What a finalization produced, for logging and tests.
#[derive(Debug, Clone, PartialEq)]
pub struct FinalizedRun {
    pub run_id: String,
    pub root_task_id: String,
    pub ai_system_id: Option<String>,
    pub terminal_state: String,
    pub chain_integrity: String,
    /// Gateway | otel | mixed.
    pub source: String,
    pub action_count: i64,
    /// DISTINCT client-asserted phases; 0 = none asserted.
    pub step_count: i64,
    pub total_cost_usd: f64,
    pub reopened: bool,
    /// The reported outcome as stored, and its judgement.
    pub outcome_id: Option<String>,
    pub outcome_conformance: String,
    pub outcome_counts_toward_baseline: Option<bool>,
    pub outcome_contract_version: Option<i32>,
    pub error_cause: Option<String>,
    pub run_class: String,
}

/// The run's failing call: the latest of its last unsuccessful
/// step and the latest delegated call whose SEGMENT carries a cause (a
/// delegate that answered with an undeclared outcome is a 200 at the proxy,
/// but a failed delegation for the caller). Returns the call and whether it
/// is the run's last action.
fn failing_call(
    agg: &RunAggregate,
    segments: &[run_segments::Segment],
) -> (Option<crate::services::run_outcome::CallFacts>, bool) {
    use crate::services::run_outcome::CallFacts;
    let cause_of = |call_id: &str| {
        segments
            .iter()
            .find(|s| s.draft.entered_by.iter().any(|e| e == call_id))
            .and_then(|s| s.error_cause)
    };
    let mut step: Option<CallFacts> = agg.failing_call.as_ref().and_then(|v| {
        serde_json::from_value::<CallFacts>(v.clone())
            .map_err(|e| tracing::warn!(error = %e, "run ledger: failing call unreadable"))
            .ok()
    });
    if let Some(c) = step.as_mut() {
        c.segment_cause = cause_of(&c.id);
    }
    let delegated = segments
        .iter()
        .filter(|s| s.error_cause.is_some())
        .filter_map(|s| {
            let id = s.draft.entered_by.last()?;
            let call = agg.delegation_calls.iter().find(|c| &c.id == id)?;
            Some(CallFacts {
                id: call.id.clone(),
                at: Some(call.at),
                proxy_type: call.proxy_type.clone(),
                proxy_subtype: call.proxy_subtype.clone(),
                http_status_code: call.http_status_code,
                guardrail_blocked: call.guardrail_blocked,
                argument_policy_denied: call.argument_policy_denied,
                segment_cause: s.error_cause,
            })
        })
        .max_by_key(|c| c.at);
    // The later one failed last; on a tie (the same row) the segment's
    // reading wins, because it knows what the delegate answered.
    let failing = match (step, delegated) {
        (Some(s), Some(d)) => Some(if s.at > d.at { s } else { d }),
        (s, d) => s.or(d),
    };
    let is_last = failing.as_ref().is_some_and(|f| f.id == agg.last_step_id);
    (failing, is_last)
}

/// Roll a run up and write it to `ai_system_runs`.
///
/// Idempotent: re-finalizing recomputes from the steps and bumps
/// `reopened_count`, which is exactly the behaviour late-arriving steps
/// need. Returns `None` when the run has no steps at all.
pub async fn finalize_run(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
    closed_by: &str,
    explicit_state: Option<&str>,
    // An outcome stated NOW (`POST /v1/runs/{id}/end`). Proxy
    // calls carry theirs on the step row (`proxy_logs.outcome_id`), which the
    // aggregate reads, so every other close path passes `None`.
    outcome_id: Option<&str>,
) -> Result<Option<FinalizedRun>, sqlx::Error> {
    let Some(agg) = aggregate_run(db, tenant_id, root_task_id).await? else {
        return Ok(None);
    };

    // --- attribution -----
    let primary_group = agg.group_ids.first().map(String::as_str);
    let attribution = resolve_ai_system(
        db,
        tenant_id,
        &AttributionInputs {
            authorizing_group_id: primary_group,
            ..Default::default()
        },
    )
    .await?;

    // Root-plus-`via`, now with segments. The run belongs to
    // the root — the owner who gets paged — and every OTHER AI System the
    // chain touched gets a segment: its own share of the run's steps, carved
    // out of the same parts the run's totals are summed from, so nothing is
    // double-counted and nothing is dropped. `via` is derived from the
    // segment set; there is no second derivation to drift from it.
    //
    // Every group resolved in ONE query; finalization runs
    // up to `SWEEP_BATCH_SIZE` times per sweeper tick.
    let nearest =
        crate::services::ai_system::nearest_ai_systems(db, tenant_id, &agg.group_ids).await?;
    let group_system: HashMap<String, String> = nearest
        .into_iter()
        .map(|(group, (system, _))| (group, system))
        .collect();
    let card_targets =
        run_segments::load_card_targets(db, tenant_id, &agg.delegation_calls).await?;
    let partition = run_segments::partition(
        &agg.parts,
        &agg.delegation_calls,
        attribution.ai_system_id.as_deref(),
        &group_system,
        &card_targets,
    );
    let via = partition.via();
    let partition_root_outcome = partition.root_outcome_id.clone();
    let segments = run_segments::finish(
        db,
        tenant_id,
        root_task_id,
        partition.segments,
        &agg.part_releases,
        agg.actor_agent_id.as_deref(),
    )
    .await?;

    // Reconcile the caller's stated outcome with any marker the client
    // left on a step. See `resolve_explicit_close`.
    let (effective_explicit, closed_by) =
        resolve_explicit_close(closed_by, explicit_state, agg.client_run_end.as_deref());

    let terminal_state = derive_terminal_state(
        TerminalInputs {
            last_success: agg.last_success,
            last_guardrail_blocked: agg.last_guardrail_blocked,
            last_policy_denied: agg.last_policy_denied,
            fallback_count: agg.fallback_count,
            error_count: agg.error_count,
            closed_by_idle: closed_by == finalized_by::IDLE || closed_by == finalized_by::SWEEPER,
        },
        effective_explicit,
    );
    let chain_integrity = derive_chain_integrity(
        agg.any_sig_failed,
        agg.any_client_dropped,
        agg.any_client_asserted,
        agg.any_external,
    );
    let run_source = derive_run_source(agg.any_external, agg.any_gateway);
    let (actor_type, actor_id) = derive_actor(&agg);
    let agent_id = if actor_type == Some("agent_identity") {
        actor_id.clone()
    } else {
        None
    };

    let run_id = format!("run-{}", ulid::Ulid::new());
    // By reference: `via` is reused for the check-context facts below.
    let via_json = json!(&via);

    // What this run already carries: an outcome stated on an
    // earlier `/end` (on no step row, so the aggregate cannot see it) and the
    // contract it is stamped with (first write wins, below).
    let prior: Option<(Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT outcome_id, contract_id FROM ai_system_runs \
          WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_optional(db)
    .await?;
    let (prior_outcome, prior_contract) = prior.unwrap_or_default();

    // Which contract governs — and judges — this run. Resolved at
    // finalization rather than per step: the request path must not pay for a
    // contract lookup. A run that straddles a promotion stays with the
    // contract it was first stamped with, for the stamp AND the judgement, so
    // a later contract change never rewrites history. A read failure fails
    // the finalization (the sweeper retries): judged without its contract,
    // every run would be stamped `undeclared`.
    let contract = match attribution.ai_system_id.as_deref() {
        Some(system_id) => {
            crate::services::run_outcome::contract_for_run(
                db,
                tenant_id,
                system_id,
                prior_contract.as_deref(),
            )
            .await?
        }
        None => None,
    };

    // The reported outcome: stated now, else the latest the
    // root's own steps carried, else one stated earlier. Judged against the
    // governing contract's catalogue.
    let reported = outcome_id
        .map(str::to_string)
        .or_else(|| partition_root_outcome.clone())
        .or(prior_outcome);
    let judgement = crate::services::run_outcome::judge(
        reported.as_deref(),
        contract.as_ref().and_then(|c| c.catalogue.as_ref()),
    );
    let (failing, failing_is_last) = failing_call(&agg, &segments);
    let error_cause = crate::services::run_outcome::derive_error_cause(
        terminal_state,
        failing.as_ref(),
        failing_is_last,
    );
    let run_class = crate::services::run_outcome::run_class(
        terminal_state,
        Some(judgement.conformance),
        judgement.counts_toward_baseline,
    );

    // The run and its segments commit together or not at all: a run whose
    // segments failed to write would break conservation for every reader.
    let mut tx = db.begin().await?;
    // Serialise this run's finalisations and read what it counted
    // toward `agent_implementation_releases` last time, BEFORE the upsert
    // overwrites it. That read is what makes a reopen idempotent.
    let prior_release =
        agent_releases::lock_and_read_prior(&mut tx, tenant_id, root_task_id).await?;
    let row: (String, i32, bool) = sqlx::query_as(UPSERT_SQL)
        .bind(&run_id)
        .bind(tenant_id)
        .bind(root_task_id)
        .bind(attribution.ai_system_id.as_deref())
        .bind(&via_json)
        .bind(attribution.system_kind.as_deref())
        .bind(actor_type)
        .bind(actor_id.as_deref())
        .bind(agent_id.as_deref())
        .bind(agg.trace_root_kind.as_deref())
        .bind(agg.started_at)
        .bind(agg.ended_at)
        .bind(agg.duration_ms)
        .bind(terminal_state)
        .bind(closed_by)
        .bind(agg.action_count)
        .bind(agg.step_count)
        .bind(agg.llm_call_count)
        .bind(agg.tool_call_count)
        .bind(agg.skill_call_count)
        .bind(agg.a2a_call_count)
        .bind(agg.turn_count)
        .bind(&agg.turn_attribution)
        .bind(agg.max_delegation_depth)
        .bind(agg.distinct_tools)
        .bind(agg.tool_signature.as_deref())
        .bind(agg.tool_set_signature.as_deref())
        .bind(agg.total_tokens)
        .bind(agg.prompt_tokens)
        .bind(agg.completion_tokens)
        .bind(agg.total_cost_units)
        .bind(agg.models_used.clone())
        .bind(agg.cache_hit_count)
        .bind(agg.tokens_saved)
        .bind(agg.error_count)
        .bind(agg.fallback_count)
        .bind(agg.tool_error_count)
        .bind(agg.tool_error_types.clone())
        .bind(agg.guardrail_trip_count)
        .bind(agg.guardrail_block_count)
        .bind(agg.policy_denial_count)
        .bind(agg.approval_request_count)
        .bind(agg.input_bytes)
        .bind(agg.output_bytes)
        .bind(agg.first_prompt_tokens)
        .bind(agg.client_label.as_deref())
        .bind(agg.rag_used)
        .bind(agg.rag_chunks)
        .bind(chain_integrity)
        .bind(contract.as_ref().map(|c| c.id.as_str()))
        .bind(contract.as_ref().map(|c| c.hash.as_str()))
        .bind(agg.subject_id.as_deref())
        .bind(agg.subject_kind.as_deref())
        .bind(agg.prompt_fingerprint.as_deref())
        .bind(agg.rag_collection_ids.as_deref())
        .bind(judgement.outcome_id.as_deref())
        .bind(run_source)
        .bind(agg.awaiting_human_ms)
        .bind(agg.active_duration_ms)
        .bind(agg.release.name.as_deref())
        .bind(agg.release.version.as_deref())
        .bind(agg.release.build.as_deref())
        .bind(agg.release.trust)
        .bind(agg.implementation_fingerprint.as_deref())
        .bind(primary_group)
        .bind(judgement.conformance)
        .bind(judgement.counts_toward_baseline)
        .bind(contract.as_ref().map(|c| c.version))
        .bind(error_cause)
        .bind(agg.peak_context_tokens)
        .bind(agg.peak_context_utilization)
        .bind(agg.context_growth)
        .bind(&agg.tools_used)
        .bind(agg.scope.scope_declared)
        .bind(agg.scope.records_touched as i32)
        .bind(agg.scope.scope_violation_count as i32)
        .bind(agg.scope.scope_unbound_count as i32)
        .fetch_one(&mut *tx)
        .await?;
    // Replay runs never feed baselines, so they carry no segments (the flag
    // lives on the stored run; the upsert never changes it).
    run_segments::replace(
        &mut tx,
        &row.0,
        tenant_id,
        root_task_id,
        attribution.ai_system_id.as_deref(),
        row.2,
        &segments,
    )
    .await?;
    // The releases this run counts toward, in the same
    // transaction: its actor's, and each segment delegate's (one per agent).
    // A replay run counts toward nothing.
    let release_now = if row.2 {
        Vec::new()
    } else {
        let actor = agent_releases::ReleaseKey::of(agent_id.as_deref(), &agg.release).map(|key| {
            agent_releases::Contribution {
                key,
                trust: agg.release.trust,
                fingerprint: agg.implementation_fingerprint.clone(),
                first_seen: agg.started_at,
                last_seen: agg.ended_at,
            }
        });
        let delegates = segments.iter().map(|s| {
            agent_releases::ReleaseKey::of(s.delegate_agent_id.as_deref(), &s.release).map(|key| {
                agent_releases::Contribution {
                    key,
                    trust: s.release.trust,
                    fingerprint: None,
                    first_seen: s.draft.first_action_at,
                    last_seen: s.draft.last_action_at,
                }
            })
        });
        agent_releases::contributions(actor, delegates)
    };
    agent_releases::account(&mut tx, tenant_id, &prior_release, &release_now).await?;
    tx.commit().await?;
    // errored/exhausted terminal states are review findings
    // (a run the gateway refused is the guardrail emitter's sharper item).
    crate::services::assurance_inbox::raise_run_failure(
        db,
        tenant_id,
        attribution.ai_system_id.as_deref(),
        &row.0,
        terminal_state,
        error_cause,
    )
    .await;

    // The judgement on the run's OTel span.
    crate::services::telemetry::record_run_judgement(&crate::services::telemetry::RunJudgement {
        run_id: &row.0,
        root_task_id,
        ai_system_id: attribution.ai_system_id.as_deref(),
        terminal_state,
        outcome_id: judgement.outcome_id.as_deref(),
        outcome_conformance: judgement.conformance,
        counts_toward_baseline: judgement.counts_toward_baseline,
        error_cause,
        run_class,
    });

    // tier-A continuous checks follow finalization.
    // Fail-open by construction: a check must never break the ledger.
    let facts = finalize_run_facts(
        &agg,
        &row.0,
        attribution.ai_system_id.as_deref(),
        attribution.system_kind.as_deref(),
        actor_type,
        agent_id.as_deref(),
        terminal_state,
        closed_by,
        chain_integrity,
        contract.is_some(),
        &via,
        row.1 > 0,
        &OutcomeFacts {
            outcome_id: judgement.outcome_id.as_deref(),
            outcome_conformance: judgement.conformance,
            error_cause,
            run_class,
        },
        run_source,
    );
    crate::services::assurance_checks::evaluate_on_finalize(
        db,
        tenant_id,
        attribution.ai_system_id.as_deref(),
        &row.0,
        &facts,
    )
    .await;

    Ok(Some(FinalizedRun {
        run_id: row.0,
        root_task_id: root_task_id.to_string(),
        ai_system_id: attribution.ai_system_id,
        terminal_state: terminal_state.to_string(),
        chain_integrity: chain_integrity.to_string(),
        source: run_source.to_string(),
        action_count: agg.action_count,
        step_count: agg.step_count,
        total_cost_usd: agg.total_cost_usd,
        reopened: row.1 > 0,
        outcome_id: judgement.outcome_id.clone(),
        outcome_conformance: judgement.conformance.to_string(),
        outcome_counts_toward_baseline: judgement.counts_toward_baseline,
        outcome_contract_version: contract.as_ref().map(|c| c.version),
        error_cause: error_cause.map(str::to_string),
        run_class: run_class.to_string(),
    }))
}

/// Upsert keyed on `(tenant_id, root_task_id)`.
///
/// `ON CONFLICT DO UPDATE` is what makes late steps work: the run is simply
/// recomputed from the (now larger) set of steps and `reopened_count` is
/// bumped. `created_at` and the original `id` are preserved so a run keeps
/// its identity across reopens.
///
/// Cost is bound as integer units of 1e-8 USD (the column scale) and scaled
/// in SQL, because the crate does not enable sqlx's decimal feature: the
/// column stays exact and equal to the sum of the run's parts.
const UPSERT_SQL: &str = r#"
INSERT INTO ai_system_runs (
    id, tenant_id, root_task_id, ai_system_id, via_system_ids, system_kind,
    actor_type, actor_id, agent_id, trace_root_kind, subject_id, subject_kind,
    started_at, ended_at, duration_ms, awaiting_human_ms, active_duration_ms,
    terminal_state, finalized_by,
    action_count, step_count, llm_call_count, tool_call_count, skill_call_count,
    a2a_call_count, turn_count, turn_attribution, max_delegation_depth, distinct_tools,
    tool_signature, tool_set_signature,
    total_tokens, prompt_tokens, completion_tokens, total_cost_usd, models_used,
    cache_hit_count, tokens_saved,
    error_count, retry_count, fallback_count, tool_error_count, tool_error_types,
    guardrail_trip_count, guardrail_block_count, policy_denial_count,
    approval_request_count,
    input_bytes, output_bytes, first_prompt_tokens,
    client_label, rag_used, rag_chunks, prompt_fingerprint, rag_collection_ids, outcome_id, source, chain_integrity, reopened_count,
    contract_id, contract_hash,
    agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_trust,
    implementation_fingerprint,
    resource_group_id,
    outcome_conformance, outcome_counts_toward_baseline, outcome_contract_version, error_cause,
    peak_context_tokens, peak_context_utilization, context_growth,
    tools_used,
    scope_declared, records_touched, scope_violation_count, scope_unbound_count,
    feedback_score
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $52, $53,
    $11, $12, $13, $58, $59,
    $14, $15,
    $16, $17, $18, $19, $20,
    $21, $22, $23, $24, $25,
    $26, $27,
    $28, $29, $30, CAST($31 AS NUMERIC) / 100000000, $32,
    $33, $34,
    $35, 0, $36, $37, $38,
    $39, $40, $41,
    $42,
    $43, $44, $45,
    $46, $47, $48, $54, $55, $56, $57, $49, 0,
    $50, $51,
    $60, $61, $62, $63,
    $64,
    -- The group the run started in (its first grouped step).
    $65,
    -- The judgement, computed in `run_outcome` from the merged
    -- outcome (stated now / on a root step / stated earlier) and the
    -- governing contract.
    $66, $67, $68, $69,
    $70, $71, $72,
    $73,
    $74, $75, $76, $77,
    -- Folded in from the source of truth rather than passed as a parameter.
    --
    -- Feedback usually arrives BEFORE the run is finalized: portal chats close
    -- on the 120s idle sweeper and users click thumbs in about two seconds, so
    -- by the time this INSERT runs the rating is often already sitting in
    -- `ai_system_run_feedback` waiting for a row to land on. Reading it here
    -- means the rollup is correct however the two race.
    --
    -- Recomputed (not preserved) on re-finalization on purpose: the sum is a
    -- pure function of the feedback table, so a reopened run and a freshly
    -- closed one arrive at the same answer.
    -- The casts are load-bearing, not decoration. $2/$3 are already used in
    -- INSERT column positions above, where Postgres deduces `varchar` from
    -- the target column; reusing them in a comparison here makes it deduce
    -- `text`, and the two deductions collide at PARSE time:
    --   ERROR: inconsistent types deduced for parameter $2
    -- Pinning the comparison to varchar keeps a single deduction. Nothing in
    -- the Rust toolchain can catch this — the statement is a plain &str, so
    -- it type-checks fine and fails only against a real database.
    (SELECT SUM(f.score)::smallint
       FROM ai_system_run_feedback f
      WHERE f.tenant_id = $2::varchar AND f.root_task_id = $3::varchar)
)
ON CONFLICT (tenant_id, root_task_id) DO UPDATE SET
    ai_system_id = EXCLUDED.ai_system_id,
    via_system_ids = EXCLUDED.via_system_ids,
    system_kind = EXCLUDED.system_kind,
    actor_type = EXCLUDED.actor_type,
    actor_id = EXCLUDED.actor_id,
    agent_id = EXCLUDED.agent_id,
    trace_root_kind = EXCLUDED.trace_root_kind,
    subject_id = EXCLUDED.subject_id,
    subject_kind = EXCLUDED.subject_kind,
    prompt_fingerprint = EXCLUDED.prompt_fingerprint,
    rag_collection_ids = EXCLUDED.rag_collection_ids,
    -- Recomputed on every re-finalization. An outcome stated on an
    -- earlier `/end` is not lost: `finalize_run` reads it back and merges it
    -- before judging, so the stored id and its judgement always agree.
    outcome_id = EXCLUDED.outcome_id,
    outcome_conformance = EXCLUDED.outcome_conformance,
    outcome_counts_toward_baseline = EXCLUDED.outcome_counts_toward_baseline,
    outcome_contract_version = EXCLUDED.outcome_contract_version,
    error_cause = EXCLUDED.error_cause,
    source = EXCLUDED.source,
    started_at = EXCLUDED.started_at,
    ended_at = EXCLUDED.ended_at,
    duration_ms = EXCLUDED.duration_ms,
    -- Recomputed with the elapsed figure on every re-finalization: a hold
    -- resolved after the first close changes how much of it was a wait.
    awaiting_human_ms = EXCLUDED.awaiting_human_ms,
    active_duration_ms = EXCLUDED.active_duration_ms,
    terminal_state = EXCLUDED.terminal_state,
    finalized_by = EXCLUDED.finalized_by,
    action_count = EXCLUDED.action_count,
    step_count = EXCLUDED.step_count,
    llm_call_count = EXCLUDED.llm_call_count,
    tool_call_count = EXCLUDED.tool_call_count,
    skill_call_count = EXCLUDED.skill_call_count,
    a2a_call_count = EXCLUDED.a2a_call_count,
    turn_count = EXCLUDED.turn_count,
    turn_attribution = EXCLUDED.turn_attribution,
    max_delegation_depth = EXCLUDED.max_delegation_depth,
    distinct_tools = EXCLUDED.distinct_tools,
    tool_signature = EXCLUDED.tool_signature,
    tool_set_signature = EXCLUDED.tool_set_signature,
    tools_used = EXCLUDED.tools_used,
    scope_declared = EXCLUDED.scope_declared,
    records_touched = EXCLUDED.records_touched,
    scope_violation_count = EXCLUDED.scope_violation_count,
    scope_unbound_count = EXCLUDED.scope_unbound_count,
    total_tokens = EXCLUDED.total_tokens,
    prompt_tokens = EXCLUDED.prompt_tokens,
    completion_tokens = EXCLUDED.completion_tokens,
    total_cost_usd = EXCLUDED.total_cost_usd,
    models_used = EXCLUDED.models_used,
    cache_hit_count = EXCLUDED.cache_hit_count,
    tokens_saved = EXCLUDED.tokens_saved,
    error_count = EXCLUDED.error_count,
    fallback_count = EXCLUDED.fallback_count,
    tool_error_count = EXCLUDED.tool_error_count,
    tool_error_types = EXCLUDED.tool_error_types,
    guardrail_trip_count = EXCLUDED.guardrail_trip_count,
    guardrail_block_count = EXCLUDED.guardrail_block_count,
    policy_denial_count = EXCLUDED.policy_denial_count,
    approval_request_count = EXCLUDED.approval_request_count,
    input_bytes = EXCLUDED.input_bytes,
    output_bytes = EXCLUDED.output_bytes,
    first_prompt_tokens = EXCLUDED.first_prompt_tokens,
    peak_context_tokens = EXCLUDED.peak_context_tokens,
    peak_context_utilization = EXCLUDED.peak_context_utilization,
    context_growth = EXCLUDED.context_growth,
    client_label = EXCLUDED.client_label,
    -- Recomputed from the steps like the actor: the
    -- earliest carrying action decides, so a late step cannot move it
    -- unless it is earlier.
    agent_impl_name = EXCLUDED.agent_impl_name,
    agent_impl_version = EXCLUDED.agent_impl_version,
    agent_impl_build = EXCLUDED.agent_impl_build,
    agent_impl_trust = EXCLUDED.agent_impl_trust,
    implementation_fingerprint = EXCLUDED.implementation_fingerprint,
    resource_group_id = EXCLUDED.resource_group_id,
    rag_used = EXCLUDED.rag_used,
    rag_chunks = EXCLUDED.rag_chunks,
    chain_integrity = EXCLUDED.chain_integrity,
    -- First write wins, deliberately. A run is governed by the contract that
    -- was active when it started; a late step arriving after a promotion must
    -- not retro-stamp it with the new one, or the audit sentence this column
    -- exists for ("every one of these runs was governed by v4") becomes false
    -- for exactly the runs that straddled the change.
    contract_id = COALESCE(ai_system_runs.contract_id, EXCLUDED.contract_id),
    contract_hash = COALESCE(ai_system_runs.contract_hash, EXCLUDED.contract_hash),
    -- Unlike the contract stamp above, this is NOT first-write-wins: EXCLUDED
    -- carries a fresh read of the feedback table, so re-finalizing picks up
    -- ratings that landed since. A COALESCE here would freeze the first
    -- rating and silently discard every later one.
    feedback_score = EXCLUDED.feedback_score,
    reopened_count = ai_system_runs.reopened_count + 1
RETURNING id, reopened_count, is_replay
"#;

// ---------------------------------------------------------------------------
// Idle sweeper
// ---------------------------------------------------------------------------

/// A run that has gone quiet and may be ready to close.
#[derive(Debug, Clone, FromRow)]
pub struct SweepCandidate {
    pub tenant_id: String,
    pub root_task_id: String,
    pub last_step_at: DateTime<Utc>,
    /// Group the steps were authorized against — resolves the AI System
    /// whose idle window applies.
    pub group_id: Option<String>,
    pub reopened_count: Option<i32>,
}

/// Candidate selection.
///
/// The idle threshold used here is the *smallest* window in play, because
/// the per-AI-System override is not known until the system is resolved.
/// Candidates are then filtered per system in [`sweep_idle_runs`]. Doing it
/// the other way round — resolving first — would mean a join per run on
/// every tick.
///
/// ## Why the ledger join sits *outside* the aggregate
///
/// This fires every 30 seconds on every replica against the busiest table
/// in the schema, so the shape matters as much as the predicate. The
/// grouping is over the whole `SWEEP_LOOKBACK_SECONDS` working set —
/// millions of rows on a real gateway — and joining `ai_system_runs` in
/// before the `GROUP BY` made every one of those rows carry a ledger row
/// with it, only for the aggregate to collapse them again.
/// `uq_ai_system_runs_root` makes the join key unique, so `MAX(r.ended_at)`
/// and `MAX(r.reopened_count)` were only ever restating that one row: the
/// join now happens once per *candidate* instead of once per *step*, which
/// is the same answer for two or three orders of magnitude less work.
///
/// The ledger predicates still apply **before** `LIMIT`, deliberately. If
/// the batch were filled with idle-but-already-finalized runs and filtered
/// afterwards, a busy tenant whose oldest 500 candidates are all closed
/// would starve every newer run out of the sweep forever.
///
/// This query is still a sequential scan without a supporting index; see
/// `ix_proxy_logs_sweep` in the control plane's migrations.
const SWEEP_CANDIDATES_SQL: &str = r#"
SELECT
    g.tenant_id,
    g.root_task_id,
    g.last_step_at,
    g.group_id,
    r.reopened_count
FROM (
    SELECT
        pl.tenant_id,
        pl.root_task_id,
        MAX(pl.timestamp)                                         AS last_step_at,
        (ARRAY_AGG(pl.resource_group_id ORDER BY pl.timestamp, pl.id)
            FILTER (WHERE pl.resource_group_id IS NOT NULL))[1]   AS group_id
    FROM proxy_logs pl
    WHERE pl.root_task_id IS NOT NULL
      AND pl.timestamp > NOW() - MAKE_INTERVAL(secs => $2)
    GROUP BY pl.tenant_id, pl.root_task_id
    HAVING MAX(pl.timestamp) < NOW() - MAKE_INTERVAL(secs => $1)
) g
LEFT JOIN ai_system_runs r
       ON r.tenant_id = g.tenant_id
      AND r.root_task_id = g.root_task_id
-- Not yet finalized, or finalized before a late step arrived.
WHERE (r.ended_at IS NULL OR g.last_step_at > r.ended_at)
  -- Stop chasing runs that keep being reopened; they are fragmented and
  -- already excluded from baselines, so more churn buys nothing.
  AND COALESCE(r.reopened_count, 0) <= $3
ORDER BY g.last_step_at ASC
LIMIT $4
"#;

/// The smallest idle window configured anywhere in the platform, used as
/// the candidate-selection threshold.
async fn min_idle_window(db: &PgPool) -> i64 {
    let row: Option<(Option<i32>,)> = sqlx::query_as(
        "SELECT MIN(run_idle_timeout_seconds) FROM resource_groups \
         WHERE run_idle_timeout_seconds IS NOT NULL AND is_active = true",
    )
    .fetch_optional(db)
    .await
    // Degrading is safe here: the default is the LARGEST window, so a
    // failed read only selects fewer candidates, and each candidate is
    // still checked against its own window below.
    .or_logged(None);

    let configured = row.and_then(|r| r.0).map(i64::from);
    match configured {
        Some(v) => v.clamp(1, DEFAULT_RUN_IDLE_TIMEOUT_SECONDS),
        None => DEFAULT_RUN_IDLE_TIMEOUT_SECONDS,
    }
}

/// The idle window that applies to a given group, walking to its AI System.
/// A database error is returned: the default window could close a run
/// its system allows to pause longer, splitting it in the evidence.
async fn idle_window_for(
    db: &PgPool,
    tenant_id: &str,
    group_id: Option<&str>,
) -> Result<i64, sqlx::Error> {
    let Some(group_id) = group_id else {
        return Ok(DEFAULT_RUN_IDLE_TIMEOUT_SECONDS);
    };
    let Some((system_id, _)) = nearest_ai_system(db, tenant_id, group_id).await? else {
        return Ok(DEFAULT_RUN_IDLE_TIMEOUT_SECONDS);
    };
    let row: Option<(Option<i32>,)> = sqlx::query_as(
        "SELECT run_idle_timeout_seconds FROM resource_groups WHERE id = $1 AND tenant_id = $2",
    )
    .bind(&system_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?;

    Ok(row
        .and_then(|r| r.0)
        .map(i64::from)
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_RUN_IDLE_TIMEOUT_SECONDS))
}

/// Close every run that has been idle past its AI System's window.
///
/// Returns how many were finalized. Never fails the caller: a sweep is a
/// best-effort background tick, and one bad run must not stop the rest.
/// Whether an operator abort is still open on this run.
///
/// Read rather than joined into `SWEEP_CANDIDATES_SQL` on purpose: the sweep
/// query is a hot, index-shaped aggregate over the busiest table in the
/// schema, and interventions are rare. One extra point lookup per candidate
/// that actually reached its idle window costs far less than widening that
/// query.
///
/// A database error is returned: read as "no abort", an operator's
/// cancellation would be sealed as an abandoned run.
async fn open_abort_exists(
    db: &PgPool,
    tenant_id: &str,
    root_task_id: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar::<_, i64>(
        "SELECT 1 FROM run_interventions \
         WHERE tenant_id = $1 AND root_task_id = $2 AND action = 'abort' \
           AND status <> 'closed' LIMIT 1",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_optional(db)
    .await
    .map(|hit| hit.is_some())
}

/// Mark an intervention closed once its run has finalized. The record stays
/// as evidence; only its lifecycle ends.
pub async fn close_intervention(db: &PgPool, tenant_id: &str, root_task_id: &str) {
    if let Err(e) = sqlx::query(
        "UPDATE run_interventions SET status = 'closed', updated_at = now() \
         WHERE tenant_id = $1 AND root_task_id = $2 AND status <> 'closed'",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .execute(db)
    .await
    {
        tracing::warn!(error = %e, tenant_id, root_task_id, "failed to close run intervention");
    }
}

pub async fn sweep_idle_runs(db: &PgPool, batch_size: i64) -> usize {
    let threshold = min_idle_window(db).await;

    let candidates: Vec<SweepCandidate> = match sqlx::query_as(SWEEP_CANDIDATES_SQL)
        .bind(threshold)
        .bind(SWEEP_LOOKBACK_SECONDS)
        .bind(MAX_REOPENS_BEFORE_FRAGMENTED)
        .bind(batch_size)
        .fetch_all(db)
        .await
    {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(error = %e, "run-ledger sweep: candidate query failed");
            return 0;
        }
    };

    // One idle-window resolution per DISTINCT (tenant,
    // group) rather than per candidate. `idle_window_for` is itself a
    // `nearest_ai_system` chain walk plus a `resource_groups` read, and a
    // batch of 500 idle runs is overwhelmingly runs from a handful of
    // systems — the same window was being re-derived hundreds of times per
    // tick. Scoped to the tick, so an operator's timeout change is picked up
    // by the next sweep; nothing is cached across ticks.
    let mut windows: HashMap<(String, Option<String>), i64> = HashMap::new();

    let mut finalized = 0usize;
    for c in candidates {
        let key = (c.tenant_id.clone(), c.group_id.clone());
        let window = match windows.get(&key) {
            Some(w) => *w,
            None => match idle_window_for(db, &c.tenant_id, c.group_id.as_deref()).await {
                Ok(w) => {
                    windows.insert(key, w);
                    w
                }
                Err(e) => {
                    // Left for the next tick rather than closed on a guess.
                    tracing::warn!(error = %e, root_task_id = %c.root_task_id, "run-ledger sweep: idle window unreadable; skipping run");
                    continue;
                }
            },
        };
        let idle_for = (Utc::now() - c.last_step_at).num_seconds();
        if idle_for < window {
            // This system is allowed to pause for longer than the global
            // threshold that selected it. Leave it for a later tick.
            continue;
        }

        // An operator's label wins over the timeout's guess.
        // A run somebody deliberately stopped is not an abandoned one, and
        // recording it as `abandoned` would lose the only fact that explains
        // the shape of its evidence. This is also how a run that was aborted
        // AFTER its last action still reaches `cancelled`: the agent had
        // already stopped, so no governed action ever came along to be
        // refused, and `steps_blocked = 0` is recorded honestly.
        let aborted = match open_abort_exists(db, &c.tenant_id, &c.root_task_id).await {
            Ok(a) => a,
            Err(e) => {
                tracing::warn!(error = %e, root_task_id = %c.root_task_id, "run-ledger sweep: abort state unreadable; skipping run");
                continue;
            }
        };
        let (closed_by, explicit_state) = if aborted {
            (finalized_by::OPERATOR, Some(terminal::CANCELLED))
        } else {
            (finalized_by::IDLE, None)
        };

        // No outcome passed: the one the client reported rides on
        // its step row (`proxy_logs.outcome_id`), which the aggregate reads,
        // so a run closed by this backstop keeps it.
        match finalize_run(
            db,
            &c.tenant_id,
            &c.root_task_id,
            closed_by,
            explicit_state,
            None,
        )
        .await
        {
            Ok(Some(run)) => {
                finalized += 1;
                tracing::debug!(
                    run_id = %run.run_id,
                    root_task_id = %run.root_task_id,
                    ai_system_id = ?run.ai_system_id,
                    terminal_state = %run.terminal_state,
                    reopened = run.reopened,
                    "run finalized by idle sweep"
                );
            }
            Ok(None) => {}
            Err(e) => {
                // A downstream dependency failing is a WARN, not an ERROR:
                // the sweep will retry on the next tick and nothing is lost.
                tracing::warn!(
                    error = %e,
                    root_task_id = %c.root_task_id,
                    "run-ledger sweep: finalize failed"
                );
            }
        }
    }
    finalized
}

/// How many runs one sweep tick will close. Bounds the work per tick so a
/// backlog is drained over several ticks instead of one long transaction
/// that competes with request traffic.
pub const SWEEP_BATCH_SIZE: i64 = 500;

// ---------------------------------------------------------------------------
// Backfill
// ---------------------------------------------------------------------------

/// Progress of a backfill pass.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BackfillProgress {
    pub scanned: usize,
    pub finalized: usize,
    pub skipped: usize,
    pub failed: usize,
}

/// Reconstruct runs from historical `proxy_logs`.
///
/// `proxy_logs` accumulates from the moment a gateway is installed,
/// regardless of when anyone enables assurance. Without this, a customer
/// who turns the feature on in month two waits three weeks for a baseline
/// while sitting on the data that would have produced one immediately —
/// and a demo shows an empty learning-mode panel instead of a populated
/// board.
///
/// Deliberately reuses [`finalize_run`], so the aggregation exists in
/// exactly one place. A separate batch implementation would be a second
/// definition of what a run *is*, free to drift from the live one, and the
/// drift would only show up as historical numbers that quietly disagree
/// with current ones.
///
/// Chunked and resumable: it walks forward through time, so re-running
/// with a later `--from` picks up where an interrupted pass stopped.
/// Backfilled runs are marked [`finalized_by::SWEEPER`] so they are
/// distinguishable from runs closed live.
pub async fn backfill_runs(
    db: &PgPool,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    batch_size: i64,
    mut on_progress: impl FnMut(&BackfillProgress),
) -> BackfillProgress {
    let mut progress = BackfillProgress::default();
    let mut cursor = from;

    loop {
        let batch: Vec<(String, String, DateTime<Utc>)> = match sqlx::query_as(
            "SELECT pl.tenant_id, pl.root_task_id, MAX(pl.timestamp) AS last_step
               FROM proxy_logs pl
              WHERE pl.root_task_id IS NOT NULL
                AND pl.timestamp >= $1 AND pl.timestamp < $2
              GROUP BY pl.tenant_id, pl.root_task_id
             HAVING MAX(pl.timestamp) > $1
              ORDER BY MAX(pl.timestamp) ASC
              LIMIT $3",
        )
        .bind(cursor)
        .bind(to)
        .bind(batch_size)
        .fetch_all(db)
        .await
        {
            Ok(rows) => rows,
            Err(e) => {
                tracing::error!(error = %e, "run backfill: batch query failed");
                progress.failed += 1;
                return progress;
            }
        };

        if batch.is_empty() {
            break;
        }

        let mut furthest = cursor;
        for (tenant_id, root_task_id, last_step) in &batch {
            progress.scanned += 1;
            if *last_step > furthest {
                furthest = *last_step;
            }

            // Never overwrite a run that was closed live: the live close
            // knows things this pass cannot, above all an explicitly
            // reported `exhausted`.
            let already: Result<Option<(String,)>, sqlx::Error> = sqlx::query_as(
                "SELECT finalized_by FROM ai_system_runs \
                 WHERE tenant_id = $1 AND root_task_id = $2",
            )
            .bind(tenant_id)
            .bind(root_task_id)
            .fetch_optional(db)
            .await;
            // Unknown is not "not closed": finalizing now could
            // overwrite a live close (an explicit `exhausted`). Skip it; a
            // later backfill pass reaches it again.
            let already = match already {
                Ok(a) => a,
                Err(e) => {
                    crate::services::db_errors::record(&e);
                    progress.failed += 1;
                    continue;
                }
            };

            if already.is_some() {
                progress.skipped += 1;
                continue;
            }

            match finalize_run(
                db,
                tenant_id,
                root_task_id,
                finalized_by::SWEEPER,
                None,
                None,
            )
            .await
            {
                Ok(Some(_)) => progress.finalized += 1,
                Ok(None) => progress.skipped += 1,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        %root_task_id,
                        "run backfill: finalize failed, continuing"
                    );
                    progress.failed += 1;
                }
            }
        }

        on_progress(&progress);

        // Advance past the batch. If every row in the batch shared one
        // timestamp we would loop forever, so nudge past it.
        cursor = if furthest > cursor {
            furthest
        } else {
            cursor + Duration::milliseconds(1)
        };
        if cursor >= to {
            break;
        }
    }

    progress
}

/// Background loop — call from `main.rs` alongside the other long-lived
/// tasks. Honours `shutdown_token` so the loop exits promptly rather than
/// waiting out the current interval.
///
/// This is the *only* mechanism that closes a run nobody told us about,
/// so it is also the thing standing between the ledger and a slow leak of
/// permanently-open runs.
pub async fn run_sweeper_loop(
    db: PgPool,
    interval_secs: u64,
    shutdown: tokio_util::sync::CancellationToken,
    // Reconciled on this tick so a replica that
    // missed both the NOTIFY and the Redis mirror still learns about an
    // abort within one sweeper interval. Bounded worst case for a control
    // whose failure mode is letting a runaway continue.
    abort_cache: Option<crate::services::run_intervention::RunAbortCache>,
) {
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(interval_secs));
    // Delay rather than burst: falling behind is better than hammering the
    // DB with catch-up sweeps after a pause.
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    tracing::info!(
        interval_secs,
        default_idle_secs = DEFAULT_RUN_IDLE_TIMEOUT_SECONDS,
        batch_size = SWEEP_BATCH_SIZE,
        "run-ledger idle sweeper started"
    );

    loop {
        tokio::select! {
            _ = shutdown.cancelled() => {
                tracing::info!("run-ledger idle sweeper received shutdown signal");
                return;
            }
            _ = ticker.tick() => {
                // Beacon before the sweep so the operator console shows
                // the loop healthy on a tick that finds nothing to close —
                // which is most ticks, and must not look like a dead loop.
                if let Err(e) = crate::services::system_ops_beacon::write_beacon(
                    &db,
                    crate::services::system_ops_beacon::schedulers::RUN_LEDGER_SWEEPER,
                ).await {
                    tracing::warn!(error = %e, "run-ledger beacon write failed");
                }

                if let Some(cache) = abort_cache.as_ref() {
                    crate::services::run_intervention::reconcile(&db, cache).await;
                }

                let started = std::time::Instant::now();
                let finalized = sweep_idle_runs(&db, SWEEP_BATCH_SIZE).await;
                if finalized > 0 {
                    // DEBUG, not INFO: runs going idle is the sweeper doing its
                    // job, and on a busy tenant this fires every tick. INFO is
                    // reserved for something an on-call engineer should act on
                    // — a routine housekeeping count is not that, and burying
                    // real signals under it is how alert fatigue starts. The
                    // count is still observable via the beacon and metrics.
                    tracing::debug!(
                        summary = %describe_sweep(finalized, started.elapsed().as_millis()),
                        "run-ledger idle sweep finalized runs"
                    );
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Run facts — the tier-A check context (a pinned wire contract)
// ---------------------------------------------------------------------------
//
// Continuous Assurance Checks evaluate bounded-CEL expressions against a
// single `run` object built here. This module owns the builders because
// "what a run looks like" has exactly one definition (this file), and
// because the phase-counter column name is legal only here (the
// vocabulary guard).
//
// Contract rules: every key is ALWAYS present — absent optionals become
// typed defaults ("" / 0 / false / []) so operator expressions never hit
// NoSuchKey. Adding a key is a minor version; removing or retyping one is
// breaking. `RUN_FACT_KEYS` is the pin; both builders are tested against
// it.

/// Version of the run-facts context schema exposed to check expressions.
///
/// 2 — `client_outcome` removed (breaking); `outcome_id`,
/// `outcome_conformance`, `error_cause` and `run_class` added.
pub const RUN_FACTS_VERSION: u32 = 2;

/// The outcome judgement the run facts carry.
#[derive(Debug, Clone, Copy)]
pub struct OutcomeFacts<'a> {
    pub outcome_id: Option<&'a str>,
    pub outcome_conformance: &'a str,
    pub error_cause: Option<&'a str>,
    pub run_class: &'a str,
}

/// Every key the `run` object carries, sorted, pinned.
pub const RUN_FACT_KEYS: &[&str] = &[
    "a2a_call_count",
    "action_count",
    "active_duration_ms",
    "actor_type",
    "agent_id",
    "agent_impl_build",
    "agent_impl_name",
    "agent_impl_trust",
    "agent_impl_version",
    "ai_system_id",
    "approval_request_count",
    "awaiting_human_ms",
    "cache_hit_count",
    "chain_integrity",
    "client_label",
    "completion_tokens",
    "contract_present",
    "distinct_tools",
    "duration_ms",
    "error_cause",
    "error_count",
    "fallback_count",
    "feedback_score",
    "finalized_by",
    "guardrail_block_count",
    "guardrail_trip_count",
    "implementation_fingerprint",
    "is_replay",
    "llm_call_count",
    "max_delegation_depth",
    "models",
    "outcome_conformance",
    "outcome_id",
    "policy_denial_count",
    "prompt_fingerprint",
    "prompt_tokens",
    "rag_chunks",
    "rag_collections",
    "rag_used",
    "records_touched",
    "reopened",
    "run_class",
    "run_id",
    "scope_declared",
    "scope_unbound_count",
    "scope_violation_count",
    "skill_call_count",
    "source",
    "step_count",
    "subject_id",
    "subject_kind",
    "system_kind",
    "terminal_state",
    "tokens_saved",
    "tool_call_count",
    "tool_error_count",
    "tools",
    "total_cost_usd",
    "total_tokens",
    "turn_count",
    "via_system_ids",
];

/// `rag_collection_ids` is a comma-separated string on the wire; facts want a list.
fn split_collections(raw: Option<&str>) -> Vec<String> {
    raw.map(|s| {
        s.split(',')
            .map(str::trim)
            .filter(|x| !x.is_empty())
            .map(String::from)
            .collect()
    })
    .unwrap_or_default()
}

/// Model names out of the `models_used` sub-aggregation
/// (`[{model, calls, ...}, ...]` → `["gpt-x", ...]`).
fn model_names(models_used: Option<&serde_json::Value>) -> Vec<String> {
    models_used
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|m| m.get("model").and_then(|s| s.as_str()))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// A JSON array of strings as a list; anything else as an empty one.
fn string_list(value: Option<&serde_json::Value>) -> Vec<String> {
    value
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|s| s.as_str())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// Build the `run` facts object at the finalization hook, where the full
/// aggregate is live.
#[allow(clippy::too_many_arguments)]
pub fn finalize_run_facts(
    agg: &RunAggregate,
    run_id: &str,
    ai_system_id: Option<&str>,
    system_kind: Option<&str>,
    actor_type: Option<&str>,
    agent_id: Option<&str>,
    terminal_state: &str,
    finalized: &str,
    chain_integrity: &str,
    contract_present: bool,
    via: &[String],
    reopened: bool,
    outcome: &OutcomeFacts<'_>,
    source: &str,
) -> serde_json::Value {
    json!({
        "run_id": run_id,
        "ai_system_id": ai_system_id.unwrap_or(""),
        "system_kind": system_kind.unwrap_or(""),
        "via_system_ids": via,
        "actor_type": actor_type.unwrap_or(""),
        "agent_id": agent_id.unwrap_or(""),
        "subject_id": agg.subject_id.as_deref().unwrap_or(""),
        "subject_kind": agg.subject_kind.as_deref().unwrap_or(""),
        "terminal_state": terminal_state,
        "finalized_by": finalized,
        "chain_integrity": chain_integrity,
        "contract_present": contract_present,
        "reopened": reopened,
        "duration_ms": agg.duration_ms,
        "active_duration_ms": agg.active_duration_ms,
        "awaiting_human_ms": agg.awaiting_human_ms,
        "action_count": agg.action_count,
        "step_count": agg.step_count,
        "turn_count": agg.turn_count,
        "llm_call_count": agg.llm_call_count,
        "tool_call_count": agg.tool_call_count,
        "skill_call_count": agg.skill_call_count,
        "source": source,
        "a2a_call_count": agg.a2a_call_count,
        "max_delegation_depth": agg.max_delegation_depth,
        "distinct_tools": agg.distinct_tools,
        "total_tokens": agg.total_tokens,
        "prompt_tokens": agg.prompt_tokens,
        "completion_tokens": agg.completion_tokens,
        "total_cost_usd": agg.total_cost_usd,
        "cache_hit_count": agg.cache_hit_count,
        "tokens_saved": agg.tokens_saved,
        "error_count": agg.error_count,
        "fallback_count": agg.fallback_count,
        "tool_error_count": agg.tool_error_count,
        "guardrail_trip_count": agg.guardrail_trip_count,
        "guardrail_block_count": agg.guardrail_block_count,
        "policy_denial_count": agg.policy_denial_count,
        "approval_request_count": agg.approval_request_count,
        "rag_used": agg.rag_used,
        "rag_chunks": agg.rag_chunks,
        "prompt_fingerprint": agg.prompt_fingerprint.as_deref().unwrap_or(""),
        "rag_collections": split_collections(agg.rag_collection_ids.as_deref()),
        "outcome_id": outcome.outcome_id.unwrap_or(""),
        "outcome_conformance": outcome.outcome_conformance,
        "error_cause": outcome.error_cause.unwrap_or(""),
        "run_class": outcome.run_class,
        "is_replay": false,
        "client_label": agg.client_label.as_deref().unwrap_or(""),
        // The release the run's actor declared, how much that
        // declaration is worth, and the software-surface fingerprint. Facts
        // for checks to DESCRIBE with: never an access input.
        "agent_impl_name": agg.release.name.as_deref().unwrap_or(""),
        "agent_impl_version": agg.release.version.as_deref().unwrap_or(""),
        "agent_impl_build": agg.release.build.as_deref().unwrap_or(""),
        "agent_impl_trust": agg.release.trust.unwrap_or(""),
        "implementation_fingerprint": agg.implementation_fingerprint.as_deref().unwrap_or(""),
        "models": model_names(agg.models_used.as_ref()),
        "tools": string_list(Some(&agg.tools_used)),
        // The run's data scope.
        "scope_declared": agg.scope.scope_declared,
        "records_touched": agg.scope.records_touched,
        "scope_violation_count": agg.scope.scope_violation_count,
        "scope_unbound_count": agg.scope.scope_unbound_count,
        "feedback_score": 0,
    })
}

/// A finalized run reloaded for backtesting — same contract as
/// [`finalize_run_facts`], sourced from the `ai_system_runs` row.
#[derive(Debug, FromRow)]
pub struct RunFactsRow {
    pub id: String,
    pub ai_system_id: Option<String>,
    pub system_kind: Option<String>,
    pub via_system_ids: Option<serde_json::Value>,
    pub actor_type: Option<String>,
    pub agent_id: Option<String>,
    pub subject_id: Option<String>,
    pub subject_kind: Option<String>,
    pub terminal_state: String,
    pub finalized_by: String,
    pub chain_integrity: String,
    pub contract_id: Option<String>,
    pub reopened_count: i32,
    pub duration_ms: Option<i32>,
    pub active_duration_ms: i32,
    pub awaiting_human_ms: i32,
    pub action_count: i32,
    pub step_count: i32,
    pub turn_count: i32,
    pub llm_call_count: i32,
    pub tool_call_count: i32,
    pub skill_call_count: i32,
    pub a2a_call_count: i32,
    pub max_delegation_depth: Option<i32>,
    pub distinct_tools: Option<i32>,
    pub total_tokens: Option<i32>,
    pub prompt_tokens: Option<i32>,
    pub completion_tokens: Option<i32>,
    pub total_cost_usd: Option<f64>,
    pub cache_hit_count: Option<i32>,
    pub tokens_saved: Option<i32>,
    pub error_count: Option<i32>,
    pub fallback_count: Option<i32>,
    pub tool_error_count: Option<i32>,
    pub guardrail_trip_count: Option<i32>,
    pub guardrail_block_count: Option<i32>,
    pub policy_denial_count: Option<i32>,
    pub approval_request_count: Option<i32>,
    pub rag_used: Option<bool>,
    pub rag_chunks: Option<i32>,
    pub prompt_fingerprint: Option<String>,
    pub rag_collection_ids: Option<String>,
    pub outcome_id: Option<String>,
    pub outcome_conformance: Option<String>,
    pub outcome_counts_toward_baseline: Option<bool>,
    pub error_cause: Option<String>,
    pub source: String,
    pub is_replay: bool,
    pub client_label: Option<String>,
    pub agent_impl_name: Option<String>,
    pub agent_impl_version: Option<String>,
    pub agent_impl_build: Option<String>,
    pub agent_impl_trust: Option<String>,
    pub implementation_fingerprint: Option<String>,
    pub models_used: Option<serde_json::Value>,
    pub tools_used: Option<serde_json::Value>,
    pub scope_declared: Option<bool>,
    pub records_touched: Option<i32>,
    pub scope_violation_count: Option<i32>,
    pub scope_unbound_count: Option<i32>,
    pub feedback_score: Option<i16>,
}

impl RunFactsRow {
    pub fn to_facts(&self) -> serde_json::Value {
        let via: Vec<String> = self
            .via_system_ids
            .as_ref()
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|s| s.as_str())
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        json!({
            "run_id": self.id,
            "ai_system_id": self.ai_system_id.as_deref().unwrap_or(""),
            "system_kind": self.system_kind.as_deref().unwrap_or(""),
            "via_system_ids": via,
            "actor_type": self.actor_type.as_deref().unwrap_or(""),
            "agent_id": self.agent_id.as_deref().unwrap_or(""),
            "subject_id": self.subject_id.as_deref().unwrap_or(""),
            "subject_kind": self.subject_kind.as_deref().unwrap_or(""),
            "terminal_state": self.terminal_state,
            "finalized_by": self.finalized_by,
            "chain_integrity": self.chain_integrity,
            "contract_present": self.contract_id.is_some(),
            "reopened": self.reopened_count > 0,
            "duration_ms": self.duration_ms.unwrap_or(0),
            "active_duration_ms": self.active_duration_ms,
            "awaiting_human_ms": self.awaiting_human_ms,
            "action_count": self.action_count,
            "step_count": self.step_count,
            "turn_count": self.turn_count,
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "skill_call_count": self.skill_call_count,
            "source": self.source,
            "a2a_call_count": self.a2a_call_count,
            "max_delegation_depth": self.max_delegation_depth.unwrap_or(0),
            "distinct_tools": self.distinct_tools.unwrap_or(0),
            "total_tokens": self.total_tokens.unwrap_or(0),
            "prompt_tokens": self.prompt_tokens.unwrap_or(0),
            "completion_tokens": self.completion_tokens.unwrap_or(0),
            "total_cost_usd": self.total_cost_usd.unwrap_or(0.0),
            "cache_hit_count": self.cache_hit_count.unwrap_or(0),
            "tokens_saved": self.tokens_saved.unwrap_or(0),
            "error_count": self.error_count.unwrap_or(0),
            "fallback_count": self.fallback_count.unwrap_or(0),
            "tool_error_count": self.tool_error_count.unwrap_or(0),
            "guardrail_trip_count": self.guardrail_trip_count.unwrap_or(0),
            "guardrail_block_count": self.guardrail_block_count.unwrap_or(0),
            "policy_denial_count": self.policy_denial_count.unwrap_or(0),
            "approval_request_count": self.approval_request_count.unwrap_or(0),
            "rag_used": self.rag_used.unwrap_or(false),
            "rag_chunks": self.rag_chunks.unwrap_or(0),
            "prompt_fingerprint": self.prompt_fingerprint.as_deref().unwrap_or(""),
            "rag_collections": split_collections(self.rag_collection_ids.as_deref()),
            "outcome_id": self.outcome_id.as_deref().unwrap_or(""),
            // A row finalized with no judgement carries `unreported`,
            // so the fact says so.
            "outcome_conformance": self
                .outcome_conformance
                .as_deref()
                .unwrap_or(crate::services::run_outcome::conformance::UNREPORTED),
            "error_cause": self.error_cause.as_deref().unwrap_or(""),
            "run_class": crate::services::run_outcome::run_class(
                &self.terminal_state,
                self.outcome_conformance.as_deref(),
                self.outcome_counts_toward_baseline,
            ),
            "is_replay": self.is_replay,
            "client_label": self.client_label.as_deref().unwrap_or(""),
            "agent_impl_name": self.agent_impl_name.as_deref().unwrap_or(""),
            "agent_impl_version": self.agent_impl_version.as_deref().unwrap_or(""),
            "agent_impl_build": self.agent_impl_build.as_deref().unwrap_or(""),
            "agent_impl_trust": self.agent_impl_trust.as_deref().unwrap_or(""),
            "implementation_fingerprint": self.implementation_fingerprint.as_deref().unwrap_or(""),
            "models": model_names(self.models_used.as_ref()),
            // NULL on a run that recorded none: nothing was recorded.
            "tools": string_list(self.tools_used.as_ref()),
            "scope_declared": self.scope_declared.unwrap_or(false),
            "records_touched": self.records_touched.unwrap_or(0),
            "scope_violation_count": self.scope_violation_count.unwrap_or(0),
            "scope_unbound_count": self.scope_unbound_count.unwrap_or(0),
            "feedback_score": self.feedback_score.unwrap_or(0),
        })
    }
}

const RUN_FACTS_SQL: &str = r#"
SELECT id, ai_system_id, system_kind, via_system_ids, actor_type, agent_id,
       subject_id, subject_kind,
       terminal_state, finalized_by, chain_integrity, contract_id,
       reopened_count, duration_ms, active_duration_ms, awaiting_human_ms,
       action_count, step_count, turn_count,
       llm_call_count, tool_call_count, skill_call_count, a2a_call_count,
       max_delegation_depth, distinct_tools, total_tokens, prompt_tokens,
       completion_tokens, total_cost_usd::float8 AS total_cost_usd,
       cache_hit_count, tokens_saved, error_count, fallback_count,
       tool_error_count, guardrail_trip_count, guardrail_block_count,
       policy_denial_count, approval_request_count, rag_used, rag_chunks,
       prompt_fingerprint, rag_collection_ids, outcome_id, outcome_conformance,
       outcome_counts_toward_baseline, error_cause, source,
       is_replay, client_label, agent_impl_name, agent_impl_version,
       agent_impl_build, agent_impl_trust, implementation_fingerprint,
       models_used, tools_used, scope_declared, records_touched,
       scope_violation_count, scope_unbound_count, feedback_score
  FROM ai_system_runs
 WHERE tenant_id = $1
   AND ended_at >= NOW() - ($2 || ' days')::interval
   AND NOT is_replay
 ORDER BY ended_at DESC
 LIMIT $3
"#;

/// Load recent finalized runs as facts, newest first — the backtest input.
pub async fn load_run_facts_window(
    db: &PgPool,
    tenant_id: &str,
    days: i32,
    limit: i64,
) -> Result<Vec<RunFactsRow>, sqlx::Error> {
    sqlx::query_as::<_, RunFactsRow>(RUN_FACTS_SQL)
        .bind(tenant_id)
        .bind(days.to_string())
        .bind(limit)
        .fetch_all(db)
        .await
}

/// Synthetic facts for author-time sample evaluation:
/// every key present, plausible values, deterministic.
pub fn sample_run_facts() -> serde_json::Value {
    json!({
        "run_id": "run-SAMPLE",
        "ai_system_id": "group-sample",
        "system_kind": "agent",
        "via_system_ids": [],
        "actor_type": "api_key",
        "agent_id": "",
        "subject_id": "",
        "subject_kind": "",
        "prompt_fingerprint": "",
        "rag_collections": [],
        "outcome_id": "task_completed",
        "outcome_conformance": "declared",
        "error_cause": "",
        "run_class": "success",
        "terminal_state": "completed",
        "finalized_by": "explicit",
        "chain_integrity": "intact",
        "contract_present": true,
        "reopened": false,
        "duration_ms": 4200,
        "active_duration_ms": 4200,
        "awaiting_human_ms": 0,
        "action_count": 6,
        "step_count": 0,
        "turn_count": 2,
        "llm_call_count": 2,
        "tool_call_count": 3,
        "skill_call_count": 0,
        "source": "gateway",
        "a2a_call_count": 1,
        "max_delegation_depth": 1,
        "distinct_tools": 2,
        "total_tokens": 3800,
        "prompt_tokens": 3000,
        "completion_tokens": 800,
        "total_cost_usd": 0.042,
        "cache_hit_count": 0,
        "tokens_saved": 0,
        "error_count": 0,
        "fallback_count": 0,
        "tool_error_count": 0,
        "guardrail_trip_count": 0,
        "guardrail_block_count": 0,
        "policy_denial_count": 0,
        "approval_request_count": 1,
        "rag_used": false,
        "rag_chunks": 0,
        "is_replay": false,
        "client_label": "",
        "agent_impl_name": "sample-agent",
        "agent_impl_version": "1.0.0",
        "agent_impl_build": "",
        "agent_impl_trust": "declared",
        "implementation_fingerprint": "",
        "models": ["sample-model"],
        "tools": ["sample_lookup", "sample_update"],
        "scope_declared": true,
        "records_touched": 1,
        "scope_violation_count": 0,
        "scope_unbound_count": 0,
        "feedback_score": 0,
    })
}

/// Human-readable rollup of a sweep, for the periodic log line.
pub fn describe_sweep(finalized: usize, elapsed_ms: u128) -> String {
    let mut parts: BTreeMap<&str, String> = BTreeMap::new();
    parts.insert("finalized", finalized.to_string());
    parts.insert("elapsed_ms", elapsed_ms.to_string());
    parts
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- run facts: the tier-A vocabulary -------------

    /// The `"key":` literals of one builder's `json!` object, read from this
    /// file so a key added to one builder and not the other — or to neither
    /// and only to RUN_FACT_KEYS — fails here.
    fn builder_keys(fn_marker: &str) -> Vec<String> {
        let src = include_str!("run_ledger.rs");
        let start = src.find(fn_marker).expect(fn_marker);
        let body = &src[start..];
        let open = body.find("json!({").expect("json! object");
        // The object closes at the first `})` line indented like the
        // `json!({` line itself.
        let line_start = body[..open].rfind('\n').map_or(0, |i| i + 1);
        let indent = &body[line_start..open];
        let close = open
            + body[open..]
                .find(&format!("\n{indent}}})"))
                .expect("end of json! object");
        let mut keys: Vec<String> = body[open..close]
            .lines()
            .filter_map(|l| {
                let l = l.trim_start();
                let rest = l.strip_prefix('"')?;
                let (key, after) = rest.split_once('"')?;
                after.starts_with(':').then(|| key.to_string())
            })
            .collect();
        keys.sort_unstable();
        keys
    }

    #[test]
    fn both_run_fact_builders_emit_exactly_the_pinned_keys() {
        for marker in ["pub fn finalize_run_facts(", "pub fn to_facts(&self)"] {
            assert_eq!(
                builder_keys(marker),
                RUN_FACT_KEYS,
                "{marker} drifted from RUN_FACT_KEYS"
            );
        }
        let mut sorted = RUN_FACT_KEYS.to_vec();
        sorted.sort_unstable();
        assert_eq!(sorted, RUN_FACT_KEYS, "RUN_FACT_KEYS stays sorted");
    }

    #[test]
    fn the_release_facts_are_pinned() {
        // Wire contract with the control plane's app/constants/assurance.py
        // (tests/test_run_facts_agent_impl.py parses this list): tier-A
        // checks can describe the release a run's actor declared.
        for key in [
            "agent_impl_name",
            "agent_impl_version",
            "agent_impl_build",
            "agent_impl_trust",
            "implementation_fingerprint",
            "client_label",
        ] {
            assert!(
                RUN_FACT_KEYS.contains(&key),
                "{key} missing from RUN_FACT_KEYS"
            );
        }
    }

    fn inputs() -> TerminalInputs {
        TerminalInputs {
            last_success: true,
            last_guardrail_blocked: false,
            last_policy_denied: false,
            fallback_count: 0,
            error_count: 0,
            closed_by_idle: false,
        }
    }

    // -- explicit close precedence ----------------------------------

    #[test]
    fn step_marker_closes_the_run_as_explicit() {
        // The regression this whole change exists for: a client set
        // `X-Brutor-Run-End: completed` on its last step, the immediate
        // finalize did not happen, and the sweeper reached the run. Without the
        // marker that produces `abandoned` / `idle` — a successful run filed
        // under the one terminal state operators are told to investigate.
        let (state, closed_by) =
            resolve_explicit_close(finalized_by::IDLE, None, Some(terminal::COMPLETED));
        assert_eq!(state, Some(terminal::COMPLETED));
        assert_eq!(closed_by, finalized_by::EXPLICIT);

        // ...and it must survive derivation, which would otherwise see
        // `closed_by_idle` and return ABANDONED.
        let mut i = inputs();
        i.closed_by_idle = true;
        assert_eq!(derive_terminal_state(i, state), terminal::COMPLETED);
    }

    #[test]
    fn caller_supplied_state_outranks_a_step_marker() {
        // `POST /v1/runs/{id}/end` is someone stating the outcome now; a
        // marker on a step is older information by construction.
        let (state, closed_by) = resolve_explicit_close(
            finalized_by::EXPLICIT,
            Some(terminal::CANCELLED),
            Some(terminal::COMPLETED),
        );
        assert_eq!(state, Some(terminal::CANCELLED));
        assert_eq!(closed_by, finalized_by::EXPLICIT);
    }

    #[test]
    fn caller_state_does_not_relabel_how_the_run_was_closed() {
        // A caller passing a state through a non-explicit path (the sweeper
        // does not, but the signature allows it) must not have the close
        // silently re-attributed to `explicit`.
        let (_, closed_by) =
            resolve_explicit_close(finalized_by::SWEEPER, Some(terminal::ERRORED), None);
        assert_eq!(closed_by, finalized_by::SWEEPER);
    }

    #[test]
    fn no_signal_leaves_derivation_and_attribution_alone() {
        let (state, closed_by) = resolve_explicit_close(finalized_by::IDLE, None, None);
        assert_eq!(state, None);
        assert_eq!(closed_by, finalized_by::IDLE);

        let mut i = inputs();
        i.closed_by_idle = true;
        assert_eq!(derive_terminal_state(i, state), terminal::ABANDONED);
    }

    #[test]
    fn every_terminal_state_survives_the_step_marker_path() {
        // The marker column is VARCHAR(24) and the middleware validates
        // against `terminal::ALL`; this pins that the two agree, so a state
        // added later cannot be silently truncated or dropped on the way
        // through.
        for s in terminal::ALL {
            assert!(s.len() <= 24, "{s} exceeds proxy_logs.run_end_state width");
            let (state, closed_by) = resolve_explicit_close(finalized_by::IDLE, None, Some(s));
            assert_eq!(state, Some(s));
            assert_eq!(closed_by, finalized_by::EXPLICIT);
            assert_eq!(derive_terminal_state(inputs(), state), s);
        }
    }

    // -- terminal state ----------------------------------------------------

    #[test]
    fn clean_run_completes() {
        assert_eq!(derive_terminal_state(inputs(), None), terminal::COMPLETED);
    }

    #[test]
    fn fallback_downgrades_to_degraded() {
        let i = TerminalInputs {
            fallback_count: 1,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::COMPLETED_DEGRADED);
    }

    #[test]
    fn recovered_error_is_degraded_not_errored() {
        // Errors happened, but the run still ended on a successful step —
        // the system recovered. Calling this `errored` would make every
        // retry look like a failure.
        let i = TerminalInputs {
            error_count: 3,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::COMPLETED_DEGRADED);
    }

    #[test]
    fn failing_last_step_is_errored() {
        let i = TerminalInputs {
            last_success: false,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ERRORED);
    }

    #[test]
    fn block_on_last_step_errors_the_run() {
        // A run the gateway stopped could not do its job — it is
        // `errored` (cause `blocked_by_policy`), like a run whose model was
        // unavailable. `blocked_policy` is no longer a terminal state.
        let i = TerminalInputs {
            last_guardrail_blocked: true,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ERRORED);

        let i = TerminalInputs {
            last_policy_denied: true,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ERRORED);

        // Even when the blocked row (wrongly) reports success.
        let i = TerminalInputs {
            last_success: true,
            last_guardrail_blocked: true,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ERRORED);
    }

    #[test]
    fn blocked_policy_is_no_terminal_state_any_more() {
        assert!(!terminal::ALL.contains(&"blocked_policy"));
        // A client (or an old marker row) naming it is ignored, never stored.
        assert_eq!(
            derive_terminal_state(inputs(), Some("blocked_policy")),
            terminal::COMPLETED
        );
    }

    #[test]
    fn mid_run_block_does_not_decide_the_outcome() {
        // Guardrail trips are counted separately; only the last step
        // decides. Otherwise a system that filters one tool result and
        // then succeeds would be reported as errored.
        let i = TerminalInputs {
            error_count: 1,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::COMPLETED_DEGRADED);
    }

    #[test]
    fn idle_close_is_abandoned_not_completed() {
        let i = TerminalInputs {
            closed_by_idle: true,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ABANDONED);
    }

    #[test]
    fn idle_close_still_reports_a_real_failure() {
        // Going quiet after a failure is an error, not an abandonment:
        // the failure is the more actionable fact.
        let i = TerminalInputs {
            closed_by_idle: true,
            last_success: false,
            ..inputs()
        };
        assert_eq!(derive_terminal_state(i, None), terminal::ERRORED);
    }

    #[test]
    fn explicit_state_wins_over_inference() {
        let i = TerminalInputs {
            closed_by_idle: true,
            ..inputs()
        };
        assert_eq!(
            derive_terminal_state(i, Some(terminal::EXHAUSTED)),
            terminal::EXHAUSTED
        );
        assert_eq!(
            derive_terminal_state(inputs(), Some(terminal::CANCELLED)),
            terminal::CANCELLED
        );
    }

    #[test]
    fn unknown_explicit_state_falls_back_to_inference() {
        // A typo from a client must not create a phantom category on the
        // terminal-state mix chart.
        assert_eq!(
            derive_terminal_state(inputs(), Some("finished-ish")),
            terminal::COMPLETED
        );
    }

    #[test]
    fn exhausted_is_never_inferred() {
        // The module contract: we cannot see max-iteration exhaustion from
        // the proxy, so no combination of derived facts may produce it.
        for last_success in [true, false] {
            for closed_by_idle in [true, false] {
                for fallback_count in [0, 2] {
                    for error_count in [0, 5] {
                        let i = TerminalInputs {
                            last_success,
                            last_guardrail_blocked: false,
                            last_policy_denied: false,
                            fallback_count,
                            error_count,
                            closed_by_idle,
                        };
                        assert_ne!(
                            derive_terminal_state(i, None),
                            terminal::EXHAUSTED,
                            "exhausted must only come from an explicit client signal"
                        );
                    }
                }
            }
        }
    }

    // -- chain integrity ---------------------------------------------------

    #[test]
    fn chain_integrity_classification() {
        assert_eq!(
            derive_chain_integrity(false, false, false, false),
            chain::INTACT
        );
        assert_eq!(
            derive_chain_integrity(false, false, true, false),
            chain::CLIENT_ASSERTED,
            "a client-correlated run is complete but not gateway-proven"
        );
        assert_eq!(
            derive_chain_integrity(false, true, false, false),
            chain::PARTIAL
        );
        assert_eq!(
            derive_chain_integrity(false, true, true, false),
            chain::PARTIAL,
            "a dropped step outranks an assertion — evidence is missing"
        );
        assert_eq!(
            derive_chain_integrity(false, false, false, true),
            chain::OBSERVED_EXTERNAL,
            "an ingested run is whole as reported, and nothing more"
        );
        assert_eq!(
            derive_chain_integrity(false, false, true, true),
            chain::OBSERVED_EXTERNAL,
            "reported outranks asserted — the weakest link names the chain"
        );
        assert_eq!(
            derive_chain_integrity(false, true, false, true),
            chain::PARTIAL,
            "a dropped step still outranks reported evidence"
        );
        assert_eq!(derive_run_source(false, true), source::GATEWAY);
        assert_eq!(derive_run_source(true, false), source::OTEL);
        assert_eq!(derive_run_source(true, true), source::MIXED);
        assert_eq!(
            derive_run_source(false, false),
            source::GATEWAY,
            "no steps at all still reads as proxied"
        );
        assert!(chain::OBSERVED_COMPLETE.contains(&chain::OBSERVED_EXTERNAL));
        assert!(chain::OBSERVED_COMPLETE_SQL.contains("observed_external"));
        assert_eq!(
            derive_chain_integrity(true, false, false, false),
            chain::BROKEN
        );
        // Tampering outranks truncation.
        assert_eq!(
            derive_chain_integrity(true, true, true, false),
            chain::BROKEN
        );
    }

    // -- wire contract -----------------------------------------------------

    #[test]
    fn terminal_states_match_the_control_plane() {
        // Mirrored in app/database/models/ai_system_run.py::TERMINAL_STATES.
        // A rename here is a wire-contract change, not a refactor.
        let mut all = terminal::ALL.to_vec();
        all.sort_unstable();
        assert_eq!(
            all,
            vec![
                "abandoned",
                "cancelled",
                "completed",
                "completed_degraded",
                "errored",
                "exhausted",
            ]
        );
    }

    #[test]
    fn finalized_by_values_are_stable() {
        assert_eq!(finalized_by::EXPLICIT, "explicit");
        assert_eq!(finalized_by::TERMINAL, "terminal");
        assert_eq!(finalized_by::IDLE, "idle");
        assert_eq!(finalized_by::SWEEPER, "sweeper");
    }

    #[test]
    fn fragmentation_cap_matches_the_control_plane() {
        assert_eq!(MAX_REOPENS_BEFORE_FRAGMENTED, 3);
    }

    #[test]
    fn default_idle_window_matches_the_control_plane() {
        assert_eq!(DEFAULT_RUN_IDLE_TIMEOUT_SECONDS, 120);
    }

    #[test]
    fn sweep_description_is_stable() {
        assert_eq!(describe_sweep(4, 12), "elapsed_ms=12 finalized=4");
    }

    /// `models_used` must carry per-model OUTCOME, not just call counts.
    ///
    /// The drift cause ladder's `model_change` rung can say the model mix
    /// moved; without these keys it cannot say which model degraded, and an
    /// operator is left opening runs by hand. Pinned as a string check on the
    /// aggregation SQL because the shape is produced by Postgres rather than
    /// by Rust — there is no struct here to assert against.
    #[test]
    fn models_used_carries_per_model_outcome() {
        for key in ["'errors'", "'blocked'", "'tokens'", "'cost'", "'p50_ms'"] {
            assert!(
                AGGREGATE_SQL.contains(key),
                "models_used lost the {key} attribution key — the drift cause \
                 ladder cannot name which model degraded without it"
            );
        }
        // Grouped per model, not globally: a run-wide error count already
        // exists as `error_count` and would be a silent duplicate here.
        assert!(
            AGGREGATE_SQL.contains("GROUP BY pl2.llm_model_used"),
            "per-model counters must be grouped by model"
        );
    }

    /// Older rows predate the outcome keys and must stay readable.
    ///
    /// The column is not backfilled — historical runs keep `{model, calls}`.
    /// Anything consuming this must treat the new keys as optional rather
    /// than assuming every row has them.
    #[test]
    fn models_used_keys_are_additive() {
        let legacy = serde_json::json!([{ "model": "gpt-5.2", "calls": 3 }]);
        let entry = &legacy[0];
        assert_eq!(entry["model"], "gpt-5.2");
        assert_eq!(entry["calls"], 3);
        // Absent keys read as Null rather than panicking — the contract every
        // reader of this column relies on.
        assert!(entry.get("errors").is_none());
        assert!(entry["p50_ms"].is_null());
    }

    // ── Feedback fold ────────────────────────────────

    #[test]
    fn finalization_folds_feedback_from_the_source_of_truth() {
        // Feedback usually lands BEFORE the run exists: portal chats close on
        // the 120s idle sweeper and users click thumbs in about two seconds.
        // If the rollup did not read the feedback table here, every rating
        // given at human speed would be silently dropped on finalization.
        assert!(
            UPSERT_SQL.contains("FROM ai_system_run_feedback f"),
            "finalization must read the feedback table, not a parameter"
        );
        assert!(
            UPSERT_SQL.contains("SUM(f.score)"),
            "the column is a rollup of every rater, not just the first"
        );
        // Scoped both ways: an unscoped sum would leak one tenant's ratings
        // into another's run.
        assert!(UPSERT_SQL.contains("f.tenant_id = $2"));
        assert!(UPSERT_SQL.contains("f.root_task_id = $3"));
    }

    #[test]
    fn refinalization_recomputes_feedback_rather_than_freezing_it() {
        // The contract stamp above it is deliberately first-write-wins. This
        // one must NOT be: a run reopened by a late step would otherwise keep
        // whatever rating existed at first close and discard every later one.
        assert!(
            UPSERT_SQL.contains("feedback_score = EXCLUDED.feedback_score"),
            "feedback must be recomputed on reopen"
        );
        assert!(
            !UPSERT_SQL.contains("COALESCE(ai_system_runs.feedback_score"),
            "a COALESCE here would freeze the first rating forever"
        );
    }

    #[test]
    fn reused_params_are_pinned_to_one_deduced_type() {
        // $2/$3 appear both as INSERT column values (deduced `varchar` from
        // the target column) and in the feedback subquery's comparison
        // (deduced `text`). Postgres refuses the collision at PARSE time:
        //   ERROR: inconsistent types deduced for parameter $2
        // It cost a live-database check to find, because a plain &str
        // statement type-checks perfectly while being unrunnable.
        assert!(
            UPSERT_SQL.contains("f.tenant_id = $2::varchar"),
            "reused param must be pinned to varchar or the statement will \
             not parse"
        );
        assert!(UPSERT_SQL.contains("f.root_task_id = $3::varchar"));
    }

    #[test]
    fn feedback_is_cast_to_the_column_type() {
        // `feedback_score` is SMALLINT; SUM() over smallint returns BIGINT in
        // Postgres, so without the cast the INSERT fails at runtime — and
        // being a plain &str, nothing would catch it at compile time.
        assert!(UPSERT_SQL.contains("SUM(f.score)::smallint"));
    }
}
