# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Run ledger API models

Mirrors `ai_system_runs`, plus the four run signals.

The signals are deliberately descriptive, not evaluative: they say what a
system *did*, not whether that was normal (that is the baselines' job).
Every field here is a count, a sum or a distribution the operator can check
by hand: nothing is claimed that the numbers do not show.
"""

from __future__ import annotations

from typing import Literal, Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RunSummary(BaseModel):
    """One row in a run list."""

    id: str
    root_task_id: str
    ai_system_id: Optional[str] = None
    ai_system_name: Optional[str] = Field(
        None, description="Resolved display name of the AI System, for list rendering"
    )
    resource_group_id: Optional[str] = Field(
        None, description="The resource group the run started in (its first step's group)"
    )
    via_system_ids: List[str] = Field(default_factory=list)
    system_kind: Optional[str] = None

    actor_type: Optional[str] = None
    actor_id: Optional[str] = None
    # Who the actor acted for (delegation subject).
    subject_id: Optional[str] = None
    subject_kind: Optional[str] = None
    # What the run was told / read; hashes and ids, never content.
    prompt_fingerprint: Optional[str] = None
    rag_collection_ids: Optional[str] = None
    # The three answers about how the run ended, and THE class
    # they combine into (`ai_system_outcomes.run_class`). `outcome_id` is the
    # agent's X-Brutor-Outcome-Id verbatim (None = nothing reported).
    outcome_id: Optional[str] = None
    outcome_conformance: Optional[Literal["declared", "undeclared", "unreported"]] = None
    outcome_counts_toward_baseline: Optional[bool] = None
    outcome_contract_version: Optional[int] = None
    run_class: Optional[Literal["success", "no_work", "deviation", "failed", "not_decisive"]] = None
    error_cause: Optional[str] = Field(
        None, description="Why the execution failed (set iff errored / exhausted)"
    )
    # WHAT software ran the run and WHICH release, from its
    # first action that carries one. `agent_impl_trust` says whether it came
    # from a verified token claim or was self-declared; describes only.
    # The normalised client label the core writes: the
    # channel (X-Brutor-Client, else the User-Agent product name without its
    # version), never the HTTP library's version string. On the list row
    # too, so the Runs table can show it (RunDetail inherits it).
    client_label: Optional[str] = None
    agent_impl_name: Optional[str] = None
    agent_impl_version: Optional[str] = None
    agent_impl_build: Optional[str] = None
    agent_impl_trust: Optional[Literal["verified", "declared"]] = None
    implementation_fingerprint: Optional[str] = None

    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = Field(
        None,
        description=(
            "Elapsed wall clock, first action to last — includes time the run "
            "spent parked awaiting a human approval"
        ),
    )
    active_duration_ms: int = Field(
        0,
        description=(
            "duration_ms minus awaiting_human_ms: the system's own execution "
            "time. What the per-run duration ceiling measures."
        ),
    )
    awaiting_human_ms: int = Field(
        0,
        description=(
            "Time the run spent awaiting a human approval decision, "
            "overlapping holds counted once"
        ),
    )

    terminal_state: str
    finalized_by: str

    #: Leaf unit — one governed call.
    action_count: int = 0
    #: DISTINCT client-asserted phases; 0 = none asserted, not "one phase".
    step_count: int = 0
    #: Passes of the agent loop. The Runs table has a Turns column that read
    #: this field while the list never sent it, so every row showed "—".
    #: Read `turn_attribution` before presenting it as reported fact.
    turn_count: int = 0
    turn_attribution: Optional[str] = None
    llm_call_count: int = 0
    tool_call_count: int = 0
    max_delegation_depth: int = 0
    distinct_tools: int = 0
    #: The names behind `distinct_tools`, attempted calls included.
    #: None when not recorded for the run (not "none used").
    tools_used: Optional[List[str]] = None
    #: The run's data scope. None when not recorded for the run.
    scope_declared: Optional[bool] = None
    records_touched: Optional[int] = None
    scope_violation_count: Optional[int] = None
    scope_unbound_count: Optional[int] = None

    total_tokens: int = 0
    total_cost_usd: float = 0.0

    error_count: int = 0
    tool_error_count: int = 0
    guardrail_block_count: int = 0
    policy_denial_count: int = 0

    chain_integrity: str
    # gateway | otel | mixed. Read it before trusting any figure above:
    # an `otel` run is the exporter's account, with no gateway control behind it.
    source: str = "gateway"
    reopened_count: int = 0
    is_replay: bool = False


class RunAction(BaseModel):
    """One governed call inside a run — the leaf of the trajectory.

    The unit the authorization plane has always called an *action*
    (`action_type`); the ledger uses the same unit. Its ancestry travels
    with it — `step_id` (phase) and `turn_id` (loop pass) — so the hierarchy
    is reconstructed by grouping, never by a recursive walk.

    A compact projection of the proxy-log row: enough to read what the run
    did, in order, without shipping request/response bodies to a list view.
    The full forensic record stays in System Logs, reachable by
    `correlation_id`.
    """

    timestamp: Optional[str] = None
    proxy_type: str
    proxy_subtype: Optional[str] = None
    capability: Optional[str] = Field(
        None, description="MCP capability / A2A capability for tool-ish actions"
    )
    model: Optional[str] = Field(None, description="Model used, for LLM actions")
    status_code: Optional[int] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    cost_usd: Optional[float] = None
    total_tokens: Optional[int] = None
    trace_continuity: Optional[str] = None
    delegation_depth: Optional[int] = None
    guardrail_blocked: Optional[bool] = None
    error_message: Optional[str] = None
    correlation_id: Optional[str] = None

    #: Ancestry, denormalised onto the leaf.
    step_id: Optional[str] = Field(None, description="Client-asserted phase; null = none asserted")
    step_name: Optional[str] = None
    turn_id: Optional[str] = None
    turn_seq: Optional[int] = None

    # --- The run overlay's facts (app/services/run_actions.py — THE
    # projection; the Runs timeline, Run details and the Composition tab's
    # run overlay all read these, so they cannot disagree). Metadata only.
    id: Optional[str] = Field(None, description="The proxy-log row id (System Logs)")
    inferred_turn: Optional[int] = Field(
        None,
        description=(
            "Gateway-inferred turn ordinal (THE trajectory rule: a non-embedding "
            "model call opens a turn). Reported beside turn_id/turn_seq, never "
            "instead of them."
        ),
    )
    number: Optional[str] = Field(
        None,
        description=(
            "The action's number in the run: 1, 2, 3 … in the gateway's order; "
            "an action that ran inside a delegation is numbered under the A2A "
            "call that entered it (5a, 5b; 5a.1 one level deeper)."
        ),
    )
    entered_by: Optional[str] = Field(
        None, description="The number of the A2A call this action ran inside; null = top level"
    )
    nesting: int = Field(0, description="Delegation nesting in the numbering (0 = top level)")
    outcome: Optional[str] = Field(
        None,
        description=(
            "answered | error | blocked | held — the connector lines' row "
            "classification (ai_system_connection_status)"
        ),
    )
    target_kind: Optional[str] = Field(
        None, description="model | mcp | skill | a2a | kb — what the action reached"
    )
    target_id: Optional[str] = Field(
        None, description="llm_models / mcp_servers / agent_skills / agent_cards / kb id"
    )
    target_name: Optional[str] = None
    kb_ids: List[str] = Field(
        default_factory=list, description="Knowledge bases the call retrieved from"
    )
    skill_script: Optional[str] = Field(
        None,
        description=(
            "A skill run's script — only a filename the skill's catalog registers"
        ),
    )
    system_id: Optional[str] = Field(
        None, description="The AI System that took the action (nearest AI System of its group)"
    )
    system_name: Optional[str] = None
    own: bool = Field(
        True, description="The run's root took it (THE own-rows rule); false = a delegate's step"
    )
    delegate_system_id: Optional[str] = Field(
        None, description="A2A calls: the AI System the call entered"
    )
    delegate_system_name: Optional[str] = None
    a2a_task_state: Optional[str] = None


class RunDetail(RunSummary):
    """A single run, with everything the rollup carries."""

    actions: List[RunAction] = Field(
        default_factory=list,
        description=(
            "The run's actions in order — the run's timeline. "
            "Capped; action_count on the summary is authoritative for totals."
        ),
    )
    agent_id: Optional[str] = None
    #: The contract version that governed this run — id and hash, so the row
    #: can be tied to the exact rules an approver signed.
    contract_id: Optional[str] = None
    contract_hash: Optional[str] = None
    trace_root_kind: Optional[str] = None

    skill_call_count: int = 0
    a2a_call_count: int = 0
    turn_count: int = 0
    turn_attribution: str = "inferred"
    tool_signature: Optional[str] = None
    tool_set_signature: Optional[str] = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    models_used: List[Dict[str, Any]] = Field(default_factory=list)
    cache_hit_count: int = 0
    tokens_saved: int = 0
    # The lowest credential level over the run's steps (null: none on the
    # ladder, or an anonymous step), whether several credentials made them,
    # and their references (first seen first, at most 8).
    credential_assurance_min: Optional[str] = None
    credential_mixed: Optional[bool] = None
    credential_refs: Optional[List[str]] = None

    retry_count: int = 0
    fallback_count: int = 0
    tool_error_types: Dict[str, int] = Field(default_factory=dict)

    guardrail_trip_count: int = 0
    approval_request_count: int = 0

    input_bytes: int = 0
    output_bytes: int = 0
    first_prompt_tokens: Optional[int] = None
    peak_context_tokens: Optional[int] = None
    peak_context_utilization: Optional[float] = None
    context_growth: Optional[float] = None

    rag_used: bool = False
    rag_chunks: int = 0

    feedback_score: Optional[int] = None
    created_at: Optional[str] = None

    # Derived, so the UI does not re-implement the rules.
    is_successful: bool = False
    is_baseline_eligible: bool = True
    is_fragmented: bool = False


class RunListResponse(BaseModel):
    runs: List[RunSummary]
    total: int
    limit: int
    offset: int


class ServedCall(BaseModel):
    """One call a shared service served inside another system's run — its
    segment of that run. The figures are the SERVICE's slice
    only; the run's own totals belong to the system that started it."""

    segment_id: str
    run_id: str
    root_task_id: str
    root_system_id: Optional[str] = None
    root_system_name: Optional[str] = None
    caller_system_id: Optional[str] = Field(None, description="Immediate calling AI System")
    caller_system_name: Optional[str] = None
    first_action_at: Optional[str] = None
    last_action_at: Optional[str] = None
    duration_ms: Optional[int] = Field(
        None, description="Summed duration of the service's own calls in the run"
    )
    action_count: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    models: List[str] = Field(default_factory=list)
    outcome: str = Field(
        ..., description="answered | rejected | failed | timed_out | blocked (gateway refusal)"
    )
    outcome_id: Optional[str] = None
    outcome_conformance: Optional[str] = None
    error_cause: Optional[str] = None
    a2a_task_state: Optional[str] = None
    agent_impl_name: Optional[str] = None
    agent_impl_version: Optional[str] = None
    guardrail_block_count: int = 0
    policy_denial_count: int = 0


class ServedCallListResponse(BaseModel):
    calls: List[ServedCall]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# The four run signals
# ---------------------------------------------------------------------------


class OutcomeCostRow(BaseModel):
    """Cost of ONE processed outcome (per full outcome id — `<outcome>` or
    `<outcome>.<variant>`), over its `success` runs only."""

    outcome_id: str
    runs: int = 0
    cost_usd: float = 0.0
    cost_per_run: Optional[float] = None
    median_cost_usd: Optional[float] = None
    p95_cost_usd: Optional[float] = None


class CostPerRunSignal(BaseModel):
    """Signal 1 — cost per *processed outcome*.

    The denominator is the point. "Cost per token" is a vendor metric; cost
    per processed outcome is the number a CFO reacts to. The population is
    the WORK: `success` runs under THE run classification
    (`ai_system_outcomes`) — completed, ended in a declared outcome that
    counts toward the baseline. Empty polls (`no_work`) are reported as
    overhead, never averaged in; errored, exhausted and deviating runs are
    *spent, nothing produced*.
    """

    processed_runs: int = Field(0, description="success runs — the population every per-run figure averages")
    total_cost_usd: float = 0.0
    cost_per_processed_outcome: Optional[float] = Field(
        None, description="null when no run processed an outcome in the window"
    )
    median_cost_per_processed_outcome: Optional[float] = None
    p95_cost_per_processed_outcome: Optional[float] = None
    by_outcome: List[OutcomeCostRow] = Field(
        default_factory=list,
        description="Per outcome / variant — never one blended average across different kinds of work",
    )
    wasted_cost_usd: float = Field(
        0.0,
        description=(
            "Spent, nothing produced: errored, exhausted and deviating runs "
            "(completed with an undeclared or no outcome)."
        ),
    )
    no_work_runs: int = Field(0, description="Completed runs whose declared outcome does not count (e.g. empty polls)")
    no_work_cost_usd: float = Field(0.0, description="No-work overhead — what the empty runs cost")
    total_tokens: int = 0
    tokens_per_processed_outcome: Optional[float] = None


class ActionDistributionBucket(BaseModel):
    lower: int
    upper: Optional[int] = Field(None, description="null on the open-ended top bucket")
    count: int


class OutcomeActionRow(BaseModel):
    """Run length of ONE processed outcome, over its `success` runs."""

    outcome_id: str
    runs: int = 0
    median_actions: Optional[float] = None
    p95_actions: Optional[float] = None
    max_actions: Optional[int] = None


class ActionCountSignal(BaseModel):
    """Signal 2 — trajectory length and exhaustion.

    A rising step count is the earliest visible sign that a system has
    started working harder for the same result. The length figures
    describe the WORK population (`success` runs), overall and per outcome —
    a one-action poll is not a short screening. Exhaustion and abandonment
    are counted over every finished run.
    """

    runs: int = Field(0, description="success runs the length figures describe")
    median_actions: Optional[float] = None
    p95_actions: Optional[float] = None
    max_actions: Optional[int] = None
    distribution: List[ActionDistributionBucket] = Field(default_factory=list)

    exhausted_runs: int = 0
    exhaustion_rate: Optional[float] = None
    exhaustion_reporting_available: bool = Field(
        False,
        description=(
            "False when no run in the window carried an explicit end "
            "signal. Exhaustion cannot be observed from the proxy — it is "
            "only ever client-reported — so a 0% rate with no reporting "
            "means 'unknown', not 'healthy'."
        ),
    )
    abandoned_runs: int = Field(
        0,
        description=(
            "Runs closed by idle timeout. Usually indicts the client, not "
            "the system — and where an unreported exhaustion lands."
        ),
    )
    finished_runs: int = Field(0, description="Every finished run — the exhaustion-rate denominator")
    by_outcome: List[OutcomeActionRow] = Field(default_factory=list)


class ToolErrorRow(BaseModel):
    tool: str
    calls: int
    errors: int
    error_rate: float
    error_types: Dict[str, int] = Field(default_factory=dict)


class ToolErrorSignal(BaseModel):
    """Signal 3 — tool error rate, per tool.

    Silent degradation of an MCP dependency surfaces here before anywhere
    else: the system keeps "working", it just retries more.
    """

    total_tool_calls: int = 0
    total_tool_errors: int = 0
    overall_error_rate: Optional[float] = None
    by_tool: List[ToolErrorRow] = Field(default_factory=list)


class StepErrorKindRow(BaseModel):
    kind: str = Field(description="model | tool | skill | agent | other")
    calls: int = 0
    errors: int = 0
    error_rate: Optional[float] = None
    status_codes: Dict[str, int] = Field(
        default_factory=dict,
        description="Errors by HTTP status code (\"none\" when the call never got one).",
    )


class StepErrorSignal(BaseModel):
    """Every failed call inside the window's runs, by kind of call.

    The tool signal above only sees MCP calls, so a model provider going
    away — every run failing on its first model call — read "0 errors" on
    the Errors tab. Counted from the
    gateway's own log rows of each run, with the gateway's definition of an
    error: a failed call, except a call held for human approval (governance
    working, not a failure).
    """

    total_calls: int = 0
    total_errors: int = 0
    overall_error_rate: Optional[float] = None
    by_kind: List[StepErrorKindRow] = Field(default_factory=list)


class TerminalStateRow(BaseModel):
    state: str
    count: int
    share: float


class TerminalStateSignal(BaseModel):
    """Signal 4 — the execution mix (how did the execution end?).

    `success_rate` is THE success rate (`ai_system_outcomes.success_rate`):
    success / (success + deviation + failed) — not completed / total.
    A policy block is an `errored` run with cause
    `blocked_by_policy` and counts against it.
    """

    total_runs: int = 0
    by_state: List[TerminalStateRow] = Field(default_factory=list)
    success_rate: Optional[float] = None


class OutcomeRow(BaseModel):
    """One reported outcome (or none) in one run class."""

    outcome_id: Optional[str] = Field(None, description="Full id as reported; null = nothing reported")
    conformance: Optional[Literal["declared", "undeclared", "unreported"]] = None
    counts_toward_baseline: Optional[bool] = None
    run_class: Literal["success", "no_work", "deviation", "failed", "not_decisive"]
    runs: int = 0
    share: float = Field(0.0, description="Share of ALL runs in the window")
    cost_usd: float = 0.0


class OutcomeSignal(BaseModel):
    """How runs ended: by declared outcome, conformance and THE
    run class. Undeclared and unreported runs are listed, never hidden;
    no-work outcomes are their own class, never success."""

    total_runs: int = 0
    success_rate: Optional[float] = None
    by_class: Dict[str, int] = Field(
        default_factory=dict, description="success | no_work | deviation | failed | not_decisive → runs"
    )
    by_outcome: List[OutcomeRow] = Field(default_factory=list)


class ErrorCauseRow(BaseModel):
    cause: str
    runs: int = 0
    share: float = Field(0.0, description="Share of failed runs")


class ErrorCauseSignal(BaseModel):
    """Why runs failed (errored / exhausted), by error cause."""

    failed_runs: int = 0
    by_cause: List[ErrorCauseRow] = Field(default_factory=list)


class ContextSignal(BaseModel):
    """How large a context the work took — a drift metric, not a health signal.

    Over the WORK population, like the action figures. `measured_runs` beside
    `runs` is the coverage: a run whose calls reported no usage says nothing
    about its context, so a figure over few measured runs must read that way.
    This is what the gateway SENT the model, not what an agent holds on its own
    side. No threshold is applied: there is no honest absolute limit, so
    movement is judged against each system's own baseline by the drift engine.
    """

    runs: int = Field(0, description="success runs in the window")
    measured_runs: int = Field(0, description="of those, runs with a measured context")
    median_peak_tokens: Optional[float] = None
    p95_peak_tokens: Optional[float] = None
    max_peak_tokens: Optional[int] = None
    median_growth: Optional[float] = None
    p95_growth: Optional[float] = None
    utilization_measured_runs: int = Field(
        0, description="measured runs whose model has a known context window"
    )
    p95_utilization: Optional[float] = None
    max_utilization: Optional[float] = None


class RunStatsResponse(BaseModel):
    """The run signal set for one AI System (or the whole tenant)."""

    ai_system_id: Optional[str] = None
    period_hours: int
    window_start: Optional[str] = None
    window_end: Optional[str] = None

    cost: CostPerRunSignal
    actions: ActionCountSignal
    context: ContextSignal = Field(default_factory=ContextSignal)
    tool_errors: ToolErrorSignal
    step_errors: StepErrorSignal = Field(default_factory=StepErrorSignal)
    terminal_states: TerminalStateSignal
    outcomes: OutcomeSignal = Field(default_factory=OutcomeSignal)
    error_causes: ErrorCauseSignal = Field(default_factory=ErrorCauseSignal)

    # Data-quality disclosure. Shown next to the numbers, never buried:
    # if a third of a system's runs have a broken chain, the operator must
    # know that before trusting anything above.
    chain_integrity_pct: Optional[float] = Field(
        None,
        description=(
            "Share of runs whose chain the gateway itself signed end to end "
            "(integrity = intact). Provenance, not coverage — see observed_pct."
        ),
    )
    # The split: one number cannot carry both questions an
    # operator asks — "did we see everything?" (coverage) and "how strong is
    # the linkage?" (provenance) — and collapsing them produced three
    # different percentages on one page. observed_pct answers coverage;
    # signed vs asserted answers provenance.
    # The share of runs whose steps were ingested from external
    # telemetry rather than proxied. Counted inside observed_pct (they are
    # complete as reported) and disclosed here so the split is never lost.
    external_pct: Optional[float] = Field(
        None,
        description=(
            "Share of runs with at least one step ingested from OpenTelemetry "
            "(chain_integrity = observed_external). Reported, not witnessed: "
            "no gateway control evidence exists for these runs."
        ),
    )
    observed_pct: Optional[float] = Field(
        None,
        description=(
            "Share of runs observed completely (intact + client_asserted). "
            "The remainder are partial/broken records."
        ),
    )
    asserted_pct: Optional[float] = Field(
        None,
        description="Share of runs grouped by a client-asserted run id",
    )
    fragmented_runs: int = 0


class RunTimeseriesPoint(BaseModel):
    """One time bucket of run activity — the chartable shape of the
    run signals. Sparse: buckets with no runs are omitted; the client
    zero-fills, because an empty bucket is real data (quiet), not a gap
    to interpolate over."""

    bucket_start: str
    runs: int = 0
    # THE run class per bucket (`ai_system_outcomes`). No-work is
    # its own series (grey, never green); there is no `blocked` series: a
    # policy block is a failed run.
    success: int = 0
    no_work: int = 0
    deviation: int = 0
    failed: int = 0
    not_decisive: int = 0
    cost_usd: float = 0.0
    processed_cost_usd: float = 0.0
    cost_per_processed_outcome: Optional[float] = None
    tool_calls: int = 0
    tool_errors: int = 0
    tool_error_rate: Optional[float] = None
    model_calls: int = 0
    model_errors: int = 0
    model_error_rate: Optional[float] = None
    # Mean, not median: per-bucket percentiles would need a probe query
    # per bucket. The headline tiles keep the exact median; the chart
    # shows shape over time and is labelled as an average. Over the work
    # population (success runs) only.
    avg_actions: Optional[float] = None


class RunTimeseriesResponse(BaseModel):
    """Bucketed run signals for charts. Same window semantics as
    /runs/stats; hourly buckets at 48h and under, daily above."""

    ai_system_id: str
    period_hours: int
    bucket: str = Field(description="hour | day")
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    points: List[RunTimeseriesPoint] = Field(default_factory=list)


class UnattributedRunGroup(BaseModel):
    """One cluster of traffic nobody has organised into an AI System yet."""

    resource_group_id: Optional[str] = None
    resource_group_name: Optional[str] = None
    runs: int = 0
    total_cost_usd: float = 0.0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    sample_root_task_ids: List[str] = Field(default_factory=list)


class UnattributedRunsResponse(BaseModel):
    """The adoption funnel.

    Unattributed runs are not an error state — they are precisely the
    traffic a customer has not yet organised into AI Systems, which makes
    this list the most useful next-step prompt in the product.
    """

    total_unattributed_runs: int = 0
    total_cost_usd: float = 0.0
    groups: List[UnattributedRunGroup] = Field(default_factory=list)
    period_hours: int = 0


# ══════════════════════════════════════════════════════════════════════
# Trajectory search
#
# The four units, in one response shape: a Run contains Steps (asserted
# phases), which contain Turns (loop passes), which contain Actions
# (governed calls). Every level is reported with its provenance, so an
# inferred number is never dressed as a reported one.
# ══════════════════════════════════════════════════════════════════════


class TrajectoryStep(BaseModel):
    """One client-asserted phase, with what it cost."""

    step_id: str
    step_name: Optional[str] = None
    turns: int = 0
    actions: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    first_at: Optional[str] = None


class TrajectoryTurn(BaseModel):
    """One pass of the agent loop, with the actions it contained."""

    turn_id: Optional[str] = None
    turn_seq: Optional[int] = None
    inferred_turn: Optional[int] = Field(
        None, description="Gateway-inferred ordinal when no turn_id was asserted"
    )
    step_id: Optional[str] = None
    step_name: Optional[str] = None
    action_count: int = 0
    tool_action_count: int = 0
    opened_by_model: Optional[str] = None
    tools: List[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    first_at: Optional[str] = None
    blocked: bool = False


class TrajectoryResponse(BaseModel):
    run_id: str
    root_task_id: Optional[str] = None
    action_count: int = 0
    turn_count: int = 0
    turn_attribution: Optional[str] = Field(
        None, description="client_asserted | inferred — never blend the two"
    )
    step_count: int = 0
    unphased: bool = Field(
        False,
        description=(
            "True when the client asserted no steps. NOT the same as 'one "
            "phase' — steps are never inferred."
        ),
    )
    steps: List[TrajectoryStep] = Field(default_factory=list)
    turns: List[TrajectoryTurn] = Field(default_factory=list)


class ConsequencePair(BaseModel):
    """A trip, and a tool action that ran shortly after it.

    Co-occurrence in execution order. The field is `actions_apart`, not
    anything implying causation: co-occurrence is not claimed as cause.
    """

    trip_action_no: int
    trip_turn_id: Optional[str] = None
    trip_step_id: Optional[str] = None
    trip_step_name: Optional[str] = None
    trip_check: Optional[str] = None
    trip_blocked: Optional[bool] = None
    trip_pii: Optional[bool] = None
    trip_policy_denied: Optional[bool] = None
    trip_at: Optional[str] = None
    after_action_no: int
    after_turn_id: Optional[str] = None
    after_step_id: Optional[str] = None
    after_step_name: Optional[str] = None
    after_type: Optional[str] = None
    after_capability: Optional[str] = None
    after_server_id: Optional[str] = None
    after_at: Optional[str] = None
    actions_apart: int


class ConsequenceResponse(BaseModel):
    run_id: str
    within: int
    pairs: List[ConsequencePair] = Field(default_factory=list)


class ActionHit(BaseModel):
    """One action found by fleet-wide search, with its ancestry attached."""

    root_task_id: Optional[str] = None
    step_id: Optional[str] = None
    step_name: Optional[str] = None
    turn_id: Optional[str] = None
    turn_seq: Optional[int] = None
    proxy_type: Optional[str] = None
    mcp_capability_name: Optional[str] = None
    llm_model_used: Optional[str] = None
    success: Optional[bool] = None
    guardrail_blocked: Optional[bool] = None
    argument_policy_denied: Optional[bool] = None
    duration_ms: Optional[float] = None
    llm_estimated_cost_usd: Optional[float] = None
    timestamp: Optional[str] = None
    correlation_id: Optional[str] = None
    ai_system_id: Optional[str] = None
    terminal_state: Optional[str] = None


class ActionSearchResponse(BaseModel):
    actions: List[ActionHit] = Field(default_factory=list)
    total: int = 0
