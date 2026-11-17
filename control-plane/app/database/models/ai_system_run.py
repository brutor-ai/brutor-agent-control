# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System Run — the keystone of AI System lifecycle assurance

A **run** is the set of `proxy_logs` rows sharing a
`(tenant_id, root_task_id)`: the unit of work an AI System performs. A user
or scheduler asks for something, the system makes N LLM calls and M tool
calls, possibly delegates, and terminates.

This table is a *rollup*. Every value in it is derived from steps the
gateway already writes — no new instrumentation on the hot path, only an
aggregation above it. Everything else in lifecycle assurance (baselines, drift detection, liveness, the assurance report) is a query
over this table.

Why the run and not the step is the unit that matters: "cost per token" is
a metric a vendor invented; "cost per completed task" is the number a CFO
reacts to, and it is the same data with the right denominator.

Written by the Rust core's run finalizer (it mints the trace chain and
sees terminal states); read by the control plane. The proxy remains
read-only with respect to *configuration* — this is telemetry, not config.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, Numeric,
    SmallInteger, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.constants.outcomes import ERROR_CAUSES, OUTCOME_CONFORMANCES
from app.database import Base


# ---------------------------------------------------------------------------
# Terminal state taxonomy
# ---------------------------------------------------------------------------
#
# A step has `success: bool`. A *run* needs an honest outcome, because
# "did every HTTP call return 200" and "did the system do its job" are
# different questions.

TERMINAL_COMPLETED = "completed"
TERMINAL_COMPLETED_DEGRADED = "completed_degraded"  # fallback / retry was used
TERMINAL_EXHAUSTED = "exhausted"                    # hit max iterations
TERMINAL_ERRORED = "errored"                        # error_cause says why
TERMINAL_ABANDONED = "abandoned"                    # idle timeout, no terminal signal
TERMINAL_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({
    TERMINAL_COMPLETED,
    TERMINAL_COMPLETED_DEGRADED,
    TERMINAL_EXHAUSTED,
    TERMINAL_ERRORED,
    TERMINAL_ABANDONED,
    TERMINAL_CANCELLED,
})
# `blocked_policy` is not a terminal state: a run the gateway
# stopped is `errored` with error_cause `blocked_by_policy`.

# The EXECUTION axis only (did the run finish?): states in which the run
# completed without errors — including the degraded case, because a run
# that needed a fallback still finished. Whether a run SUCCEEDED also needs
# its declared outcome: that is decided once, in
# `app/services/ai_system_outcomes.py` (static guard:
# tests/test_run_success_single_definition.py).
SUCCESSFUL_TERMINAL_STATES = frozenset({
    TERMINAL_COMPLETED,
    TERMINAL_COMPLETED_DEGRADED,
})


# ---------------------------------------------------------------------------
# How the run was closed
# ---------------------------------------------------------------------------
#
# No LLM or MCP protocol has an end-of-run signal, so finalization has
# three mechanisms in precedence order.

FINALIZED_EXPLICIT = "explicit"   # client sent X-Brutor-Run-End / POST …/end
FINALIZED_TERMINAL = "terminal"   # a step the gateway knows is terminal
FINALIZED_IDLE = "idle"           # no new step within the idle window
FINALIZED_SWEEPER = "sweeper"     # closed by the backfill / catch-up pass
#: An operator stopped this run. Kept distinct from `idle`
#: and `sweeper` so the operator's label wins over the timeout's guess: a run
#: somebody deliberately stopped is not an abandoned one.
FINALIZED_OPERATOR = "operator"

FINALIZED_BY = frozenset({
    FINALIZED_EXPLICIT,
    FINALIZED_TERMINAL,
    FINALIZED_IDLE,
    FINALIZED_SWEEPER,
    FINALIZED_OPERATOR,
})


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------
#
# `proxy_logs.trace_continuity` records how each step joined the signed
# chain. A run containing a dropped or forged link is only *partly*
# observed, and such runs are excluded from baselines —
# polluting a baseline with fragments produces false drift, which is the
# single most likely way this feature loses an operator's trust in week
# one.

CHAIN_INTACT = "intact"
# Complete, but the step linkage is the client's word (`x-brutor-run-id`)
# rather than the gateway's signature. Wire value produced by
# gateway-core's `run_ledger::chain::CLIENT_ASSERTED`.
CHAIN_CLIENT_ASSERTED = "client_asserted"
CHAIN_PARTIAL = "partial"    # at least one step's client dropped the chain
CHAIN_BROKEN = "broken"      # a signature failed verification
# At least one step was ingested from external telemetry (the OTel
# inlet). Complete as *reported*: the exporter's trace id grouped the steps
# and nothing is known to be missing, but the gateway neither signed the
# linkage nor sat in the path. Weaker than `client_asserted`, which at least
# binds the assertion to a credential the gateway issued. Wire value produced
# by gateway-core's `run_ledger::chain::OBSERVED_EXTERNAL`.
CHAIN_OBSERVED_EXTERNAL = "observed_external"

CHAIN_INTEGRITIES = frozenset(
    {CHAIN_INTACT, CHAIN_CLIENT_ASSERTED, CHAIN_OBSERVED_EXTERNAL, CHAIN_PARTIAL, CHAIN_BROKEN}
)

# Provenance of a run's steps. `gateway`: every step proxied.
# `otel`: every step ingested. `mixed`: both — an agent whose model calls
# ran through the gateway but whose tool spans arrived by export, say.
# Mirrors `run_ledger::source` in gateway-core.
RUN_SOURCE_GATEWAY = "gateway"
RUN_SOURCE_OTEL = "otel"
RUN_SOURCE_MIXED = "mixed"
RUN_SOURCES = frozenset({RUN_SOURCE_GATEWAY, RUN_SOURCE_OTEL, RUN_SOURCE_MIXED})

# The values that mean the WHOLE run was observed — every step present and
# correctly grouped. This is the eligibility rule for anything computing
# over complete runs (baselines, replay suites, conformance windows).
#
# `client_asserted` belongs here: fragments are excluded because half
# a run looks like a short run and manufactures drift, but an asserted run
# is complete — its provenance is weaker (disclosed as a split, never
# collapsed into "intact"), not its coverage. Excluding it would starve
# baselines for run-id-correlated agents, whose traffic is 100% asserted.
# `observed_external` belongs here for the same reason: an ingested run is
# whole as reported, so it can teach a baseline what the system normally
# does. What it cannot do is evidence any *control* — the report says so in
# `not_covered`, and a verdict resting only on ingested runs is capped at
# the observed-history basis.
# Mirrors `run_ledger::chain::OBSERVED_COMPLETE` in gateway-core.
CHAIN_OBSERVED_COMPLETE = (CHAIN_INTACT, CHAIN_CLIENT_ASSERTED, CHAIN_OBSERVED_EXTERNAL)

# Runs reopened more than this many times by late-arriving steps are
# flagged fragmented and excluded from baselines. Not an error — a data
# quality signal.
MAX_REOPENS_BEFORE_FRAGMENTED = 3

# Default idle window before a run with no terminal signal is closed.
# Overridable per AI System via `resource_groups.run_idle_timeout_seconds`
# — an `integration` may legitimately pause for minutes between steps.
DEFAULT_RUN_IDLE_TIMEOUT_SECONDS = 120



class AISystemRun(Base):
    """One finalized run. See module docstring."""

    __tablename__ = "ai_system_runs"

    id = Column(String(64), primary_key=True)  # run-<ULID>

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    root_task_id = Column(
        String(255),
        nullable=False,
        comment="Groups the steps of this run in proxy_logs (the call chain)",
    )

    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "The AI System this run belongs to — a resource group with "
            "group_type='ai_system'. NULL means unattributed, "
            "which is a first-class state surfaced in the fleet board's "
            "adoption funnel, not an error."
        ),
    )

    resource_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "The resource group the run started in (its first step's "
            "group). Lets an unattributed run say whose traffic it is, and "
            "lets platform calls (the Unbound group) be told apart from runs "
            "that could belong to an AI System. NULL when it is not known."
        ),
    )

    via_system_ids = Column(
        JSONB,
        nullable=True,
        comment=(
            "Other AI Systems this chain traversed (root-plus-via). The run is attributed to the root system — "
            "the owner who gets paged — while the shared services it "
            "called are recorded here. Splitting into linked runs would "
            "double-count cost and make cost-per-completed-task ambiguous."
        ),
    )

    system_kind = Column(
        String(24),
        nullable=True,
        comment="Denormalized from the AI System; selects the signal profile",
    )

    # ------------------------------------------------------------------
    # Actor — a dimension, not the key
    # ------------------------------------------------------------------

    actor_type = Column(
        String(24),
        nullable=True,
        comment="agent_identity | end_user | api_key | scheduler | external_system",
    )
    actor_id = Column(String(255), nullable=True)

    agent_id = Column(
        String(255),
        ForeignKey("agent_identities.id", ondelete="SET NULL"),
        nullable=True,
        comment="Convenience FK when actor_type='agent_identity'",
    )

    # The subject: who the actor acted FOR. The proxy stamps it per
    # step from the signed delegation chain (`x-brutor-delegation-subject`)
    # or the api-key user. Without it, once an agent is the actor the human
    # vanishes from the run. Carried here so it is a run fact, a Tier-A check field and a column in the
    # Runs table — the answer to "which person did this agent act for".
    subject_id = Column(String(255), nullable=True)
    subject_kind = Column(String(16), nullable=True, comment="user | agent | service")

    ai_asset_id = Column(
        String(255),
        nullable=True,
        comment=(
            "Advisory: the primary asset this run "
            "exercised, where one is identifiable. Not the attribution key "
            "— ai_assets is the inventory of things, the AI System is the "
            "governed composition."
        ),
    )

    # ------------------------------------------------------------------
    # Governing contract
    # ------------------------------------------------------------------

    contract_id = Column(String(64), nullable=True)
    # 80, not 64: the stamped value is `sha256:` + 64 hex (71 chars), the
    # same format ai_system_contracts.contract_hash stores. At 64 the stamped
    # value overflows and live finalization fails.
    contract_hash = Column(String(80), nullable=True)

    trace_root_kind = Column(
        String(32),
        nullable=True,
        comment="llm_user | agent_invocation | skill_execution | scheduled_job | external_api",
    )

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    #: Elapsed wall clock, first step to last — INCLUDING any time the run sat
    #: parked on a human approval (a held call is retried on the same run once
    #: a person decides, possibly hours later).
    duration_ms = Column(Integer, nullable=True)
    #: Time inside [started_at, ended_at] covered by the run's approval
    #: holds (`tool_approval_requests` joined on originating_trace_root_id =
    #: root_task_id), overlapping holds merged. Written by the core finalizer.
    awaiting_human_ms = Column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Time parked awaiting a human approval (ms), holds merged",
    )
    #: duration_ms − awaiting_human_ms: the agent's own execution time.
    #: The per-run duration ceiling and the duration drift baseline read THIS,
    #: not duration_ms — waiting for a person is governance, not runtime.
    active_duration_ms = Column(
        Integer, nullable=False, default=0, server_default="0",
        comment="duration_ms minus awaiting_human_ms",
    )

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------

    terminal_state = Column(String(24), nullable=False)
    finalized_by = Column(String(16), nullable=False)

    # ------------------------------------------------------------------
    # Shape — the trajectory signals
    # ------------------------------------------------------------------

    #: Leaf unit of execution — one governed call (LLM · MCP · Skill · A2A).
    #: Named `action` to match the authorization plane, which has decided
    #: `action_type`.
    action_count = Column(Integer, nullable=False, default=0, server_default="0")
    llm_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    tool_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    skill_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    a2a_call_count = Column(Integer, nullable=False, default=0, server_default="0")
    #: Passes of the agent loop. Read `turn_attribution` before
    #: treating this as a number the client reported.
    turn_count = Column(Integer, nullable=False, default=0, server_default="0")
    #: "client_asserted" | "inferred" — mirrors chain_integrity: an inferred
    #: count must never be presented as though the client reported it.
    turn_attribution = Column(String(16), nullable=False, default="inferred",
                              server_default="inferred")
    #: DISTINCT client-asserted phases. 0 means the client asserted
    #: none — never that the run had a single phase. Steps are never inferred.
    step_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_delegation_depth = Column(Integer, nullable=False, default=0, server_default="0")
    distinct_tools = Column(Integer, nullable=False, default=0, server_default="0")
    # The names behind `distinct_tools`, attempted calls included. NULL
    # means not recorded, which is not "used none".
    tools_used = Column(JSONB, nullable=True)
    # The run's data scope. NULL: not recorded.
    scope_declared = Column(Boolean, nullable=True)
    records_touched = Column(Integer, nullable=True)
    scope_violation_count = Column(Integer, nullable=True)
    scope_unbound_count = Column(Integer, nullable=True)

    tool_signature = Column(
        String(64),
        nullable=True,
        comment=(
            "Hash of the ORDERED tool sequence — trajectory shape in one "
            "indexable column. Changes when the order changes."
        ),
    )
    tool_set_signature = Column(
        String(64),
        nullable=True,
        comment=(
            "Hash of the UNORDERED tool set. Comparing the two "
            "distinguishes 'it uses different tools' from 'it uses the "
            "same tools in a different order' — different diagnoses."
        ),
    )

    # ------------------------------------------------------------------
    # Cost — the denominator that matters
    # ------------------------------------------------------------------

    total_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    prompt_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens = Column(Integer, nullable=False, default=0, server_default="0")

    total_cost_usd = Column(
        Numeric(18, 8),
        nullable=False,
        default=0,
        server_default="0",
        comment="Cost per completed run lives here — the CFO-facing number",
    )

    models_used = Column(
        JSONB,
        nullable=True,
        comment="[{model, calls}] — feeds model-drift attribution",
    )

    cache_hit_count = Column(Integer, nullable=False, default=0, server_default="0")
    tokens_saved = Column(Integer, nullable=False, default=0, server_default="0")

    # ------------------------------------------------------------------
    # Reliability
    # ------------------------------------------------------------------

    error_count = Column(Integer, nullable=False, default=0, server_default="0")
    retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    fallback_count = Column(Integer, nullable=False, default=0, server_default="0")

    tool_error_count = Column(Integer, nullable=False, default=0, server_default="0")
    tool_error_types = Column(
        JSONB,
        nullable=True,
        comment="{error_type: count} — silent degradation of an MCP dependency surfaces here first",
    )

    # ------------------------------------------------------------------
    # Governance outcomes
    # ------------------------------------------------------------------

    guardrail_trip_count = Column(Integer, nullable=False, default=0, server_default="0")
    guardrail_block_count = Column(Integer, nullable=False, default=0, server_default="0")
    policy_denial_count = Column(Integer, nullable=False, default=0, server_default="0")
    approval_request_count = Column(Integer, nullable=False, default=0, server_default="0")

    # ------------------------------------------------------------------
    # Input-distribution signals — the drift disambiguator
    # ------------------------------------------------------------------

    input_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    output_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    first_prompt_tokens = Column(
        Integer,
        nullable=True,
        comment=(
            "Context of the first model call, cached tokens included — proxies the size of the "
            "task as posed, before the system's own context accumulates. "
            "Moves when the *questions* change rather than the system."
        ),
    )
    # Context size. NULL is "not measured" (no call reported usage, or
    # no known window), never zero.
    peak_context_tokens = Column(
        Integer,
        nullable=True,
        comment="The largest context any one call in the run sent its model, cached tokens included",
    )
    peak_context_utilization = Column(
        Float,
        nullable=True,
        comment="The largest share of a model's context window any call used; NULL when no window is known",
    )
    context_growth = Column(
        Float,
        nullable=True,
        comment="The peak over the first measured context of the same agent",
    )

    # ------------------------------------------------------------------
    # Conformance
    # ------------------------------------------------------------------

    # The NORMALISED client label written by the core
    # (X-Brutor-Client, else the first User-Agent product name without its
    # version): a channel, never the HTTP library's version string.
    client_label = Column(String(255), nullable=True)
    client_user_agent_hash = Column(String(64), nullable=True)

    # WHAT software ran this and WHICH release, from
    # the run's first action that carries one (the same rule as the actor).
    # Describes only; never read by any access decision.
    agent_impl_name = Column(
        String(64), nullable=True,
        comment="Release name of the run's first action that carries one",
    )
    agent_impl_version = Column(String(64), nullable=True)
    agent_impl_build = Column(String(128), nullable=True)
    agent_impl_trust = Column(String(16), nullable=True, comment="verified | declared")
    implementation_fingerprint = Column(
        String(71), nullable=True,
        comment="sha256: + 64 hex over the first action's UA, MCP clientInfo "
        "and instruction fingerprint",
    )

    rag_used = Column(Boolean, nullable=False, default=False, server_default="false")
    rag_chunks = Column(Integer, nullable=False, default=0, server_default="0")

    # What the run was told, and what it read. The dominant instruction
    # fingerprint across its model calls and the collection set it retrieved
    # from — the two no-release doors the cause ladder can now name.
    prompt_fingerprint = Column(String(64), nullable=True)
    rag_collection_ids = Column(String(1024), nullable=True)

    # ------------------------------------------------------------------
    # Declared outcome — what the run achieved, whether it counts, and why it failed
    # ------------------------------------------------------------------

    #: The agent's `X-Brutor-Outcome-Id`, verbatim (`<outcome>[.<variant>]`;
    #: a malformed value truncated to 128). NULL = nothing reported.
    outcome_id = Column(
        String(128), nullable=True,
        comment="X-Brutor-Outcome-Id as reported, verbatim",
    )
    #: declared | undeclared | unreported, judged at finalize against the
    #: active contract's catalogue. NULL only while the run is open.
    outcome_conformance = Column(
        String(16), nullable=True,
        comment="declared | undeclared | unreported",
    )
    #: The catalogue's word on a DECLARED outcome; NULL otherwise.
    outcome_counts_toward_baseline = Column(
        Boolean, nullable=True,
        comment="From the catalogue when declared, else NULL",
    )
    #: The active contract version the outcome was judged against (NULL =
    #: none), so a later contract change never rewrites history.
    outcome_contract_version = Column(Integer, nullable=True)
    #: Why the execution failed — set iff terminal_state ∈ {errored, exhausted}.
    error_cause = Column(
        String(32), nullable=True,
        comment="Set iff terminal_state is errored or exhausted",
    )

    # ------------------------------------------------------------------
    # Data quality
    # ------------------------------------------------------------------

    chain_integrity = Column(
        String(24),  # 24, not 16: `observed_external`
        nullable=False,
        default=CHAIN_INTACT,
        server_default=CHAIN_INTACT,
    )
    reopened_count = Column(Integer, nullable=False, default=0, server_default="0")

    # See RUN_SOURCES. Derived by the ledger from the steps' `source`.
    source = Column(
        String(16),
        nullable=False,
        default=RUN_SOURCE_GATEWAY,
        server_default=RUN_SOURCE_GATEWAY,
        comment="gateway | otel | mixed — provenance of the run's steps",
    )

    is_replay = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Replay runs are excluded from baselines",
    )

    feedback_score = Column(
        SmallInteger,
        nullable=True,
        comment="Portal thumbs — the only true-outcome signal we will ever have",
    )

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "root_task_id", name="uq_ai_system_runs_root"),
        Index("ix_ai_system_runs_system_started", "tenant_id", "ai_system_id", "started_at"),
        Index("ix_ai_system_runs_terminal", "tenant_id", "terminal_state", "started_at"),
        Index("ix_ai_system_runs_contract", "tenant_id", "contract_hash"),
        Index("ix_ai_system_runs_tool_sig", "tenant_id", "ai_system_id", "tool_signature"),
        Index("ix_ai_system_runs_actor", "tenant_id", "actor_type", "actor_id"),
        # Evidence source; mirrors the migration's index name exactly.
        Index("ix_ai_system_runs_tenant_source", "tenant_id", "source"),
        # The run's starting group; mirrors the migration's index name.
        Index("ix_ai_system_runs_group_started", "tenant_id", "resource_group_id", "started_at"),
        CheckConstraint(
            "agent_impl_trust IN ('verified', 'declared')",
            name="ck_ai_system_runs_agent_impl_trust",
        ),
        {
            "comment": (
                "One row per finalized run. Rollup over "
                "proxy_logs grouped by root_task_id; written by the Rust "
                "core's finalizer."
            )
        },
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @validates("terminal_state")
    def _validate_terminal_state(self, _key, value):
        if value is not None and value not in TERMINAL_STATES:
            raise ValueError(
                f"terminal_state must be one of {sorted(TERMINAL_STATES)}, got {value!r}"
            )
        return value

    @validates("finalized_by")
    def _validate_finalized_by(self, _key, value):
        if value is not None and value not in FINALIZED_BY:
            raise ValueError(
                f"finalized_by must be one of {sorted(FINALIZED_BY)}, got {value!r}"
            )
        return value

    @validates("chain_integrity")
    def _validate_chain_integrity(self, _key, value):
        if value is not None and value not in CHAIN_INTEGRITIES:
            raise ValueError(
                f"chain_integrity must be one of {sorted(CHAIN_INTEGRITIES)}, "
                f"got {value!r}"
            )
        return value

    @validates("outcome_conformance")
    def _validate_outcome_conformance(self, _key, value):
        if value is not None and value not in OUTCOME_CONFORMANCES:
            raise ValueError(
                f"outcome_conformance must be one of {list(OUTCOME_CONFORMANCES)}, got {value!r}"
            )
        return value

    @validates("error_cause")
    def _validate_error_cause(self, _key, value):
        if value is not None and value not in ERROR_CAUSES:
            raise ValueError(f"error_cause must be one of {list(ERROR_CAUSES)}, got {value!r}")
        return value

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def is_successful(self) -> bool:
        """Did the system do the work it was asked to do?

        THE classification: completed without errors, ended in a
        declared outcome, and that outcome counts toward the baseline.
        """
        from app.services.ai_system_outcomes import RUN_SUCCESS

        return self.run_class == RUN_SUCCESS

    @property
    def run_class(self) -> str:
        """success | no_work | deviation | failed | not_decisive — read off
        `app/services/ai_system_outcomes.run_class_of`, never recomputed."""
        from app.services.ai_system_outcomes import run_class_of

        return run_class_of(self)

    @property
    def is_baseline_eligible(self) -> bool:
        """Whether this run may inform a behavioural baseline.

        Fragments and replays would teach the detector a shape that never
        happened.
        """
        return (
            self.chain_integrity in CHAIN_OBSERVED_COMPLETE
            and not self.is_replay
            and (self.reopened_count or 0) <= MAX_REOPENS_BEFORE_FRAGMENTED
        )

    @property
    def is_fragmented(self) -> bool:
        return (self.reopened_count or 0) > MAX_REOPENS_BEFORE_FRAGMENTED

    def __repr__(self) -> str:
        return (
            f"<AISystemRun(id='{self.id}', system='{self.ai_system_id}', "
            f"state='{self.terminal_state}', actions={self.action_count})>"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "root_task_id": self.root_task_id,
            "ai_system_id": self.ai_system_id,
            "resource_group_id": self.resource_group_id,
            "via_system_ids": self.via_system_ids or [],
            "system_kind": self.system_kind,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "agent_id": self.agent_id,
            "ai_asset_id": self.ai_asset_id,
            # Subject-on-run. Who the run was ABOUT, as distinct
            # from who ran it; a data-subject erasure request is answered from
            # this pair, so a serialiser that drops it makes runs invisible to
            # the process that has to find them.
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind,
            "contract_id": self.contract_id,
            "contract_hash": self.contract_hash,
            "trace_root_kind": self.trace_root_kind,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "active_duration_ms": self.active_duration_ms,
            "awaiting_human_ms": self.awaiting_human_ms,
            "terminal_state": self.terminal_state,
            # The declared outcome and why an execution failed.
            # Not derivable from `terminal_state`: a consumer that builds a
            # response from `to_dict()` must never lose them.
            "outcome_id": self.outcome_id,
            "outcome_conformance": self.outcome_conformance,
            "outcome_counts_toward_baseline": self.outcome_counts_toward_baseline,
            "outcome_contract_version": self.outcome_contract_version,
            "error_cause": self.error_cause,
            "run_class": self.run_class,
            "finalized_by": self.finalized_by,
            "action_count": self.action_count,
            "step_count": self.step_count,
            "llm_call_count": self.llm_call_count,
            "tool_call_count": self.tool_call_count,
            "skill_call_count": self.skill_call_count,
            "a2a_call_count": self.a2a_call_count,
            "turn_count": self.turn_count,
            "turn_attribution": self.turn_attribution,
            "max_delegation_depth": self.max_delegation_depth,
            "distinct_tools": self.distinct_tools,
            "tools_used": self.tools_used,
            "scope_declared": self.scope_declared,
            "records_touched": self.records_touched,
            "scope_violation_count": self.scope_violation_count,
            "scope_unbound_count": self.scope_unbound_count,
            "tool_signature": self.tool_signature,
            "tool_set_signature": self.tool_set_signature,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_cost_usd": float(self.total_cost_usd or 0),
            "models_used": self.models_used or [],
            "cache_hit_count": self.cache_hit_count,
            "tokens_saved": self.tokens_saved,
            "error_count": self.error_count,
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "tool_error_count": self.tool_error_count,
            "tool_error_types": self.tool_error_types or {},
            "guardrail_trip_count": self.guardrail_trip_count,
            "guardrail_block_count": self.guardrail_block_count,
            "policy_denial_count": self.policy_denial_count,
            "approval_request_count": self.approval_request_count,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "first_prompt_tokens": self.first_prompt_tokens,
            "peak_context_tokens": self.peak_context_tokens,
            "peak_context_utilization": self.peak_context_utilization,
            "context_growth": self.context_growth,
            "client_label": self.client_label,
            "client_user_agent_hash": self.client_user_agent_hash,
            "agent_impl_name": self.agent_impl_name,
            "agent_impl_version": self.agent_impl_version,
            "agent_impl_build": self.agent_impl_build,
            "agent_impl_trust": self.agent_impl_trust,
            "implementation_fingerprint": self.implementation_fingerprint,
            "rag_used": self.rag_used,
            "rag_chunks": self.rag_chunks,
            "rag_collection_ids": self.rag_collection_ids,
            "prompt_fingerprint": self.prompt_fingerprint,
            "chain_integrity": self.chain_integrity,
            "source": self.source or RUN_SOURCE_GATEWAY,
            "reopened_count": self.reopened_count,
            "is_replay": self.is_replay,
            "feedback_score": self.feedback_score,
            "is_successful": self.is_successful,
            "is_baseline_eligible": self.is_baseline_eligible,
            "is_fragmented": self.is_fragmented,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


__all__ = [
    "AISystemRun",
    "TERMINAL_STATES",
    "SUCCESSFUL_TERMINAL_STATES",
    "TERMINAL_COMPLETED",
    "TERMINAL_COMPLETED_DEGRADED",
    "TERMINAL_EXHAUSTED",
    "TERMINAL_ERRORED",
    "TERMINAL_ABANDONED",
    "TERMINAL_CANCELLED",
    "FINALIZED_OPERATOR",
    "FINALIZED_BY",
    "FINALIZED_EXPLICIT",
    "FINALIZED_TERMINAL",
    "FINALIZED_IDLE",
    "FINALIZED_SWEEPER",
    "CHAIN_CLIENT_ASSERTED",
    "CHAIN_OBSERVED_EXTERNAL",
    "CHAIN_INTEGRITIES",
    "RUN_SOURCE_GATEWAY",
    "RUN_SOURCE_OTEL",
    "RUN_SOURCE_MIXED",
    "RUN_SOURCES",
    "CHAIN_OBSERVED_COMPLETE",
    "CHAIN_INTACT",
    "CHAIN_PARTIAL",
    "CHAIN_BROKEN",
    "MAX_REOPENS_BEFORE_FRAGMENTED",
    "DEFAULT_RUN_IDLE_TIMEOUT_SECONDS",
]
