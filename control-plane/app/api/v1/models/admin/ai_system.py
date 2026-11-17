# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System fleet board models

An AI System is a Resource Group with `group_type='ai_system'`.
This module carries the *fleet* view of them — the board an operator opens
to see every governed AI System in one place, with the governance
attributes on the RG (owner, intended use, lifecycle stage, risk
tier) plus the composition counts.

The board deliberately does not invent its own storage. Everything here is
projected from `resource_groups` and from the run ledger, which fills in
the health, drift and liveness columns.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.api.v1.models.admin.resource_group import (
    EuAiActRiskTier,
    LifecycleStage,
    SystemKind,
)


class AISystemMembers(BaseModel):
    """What the system is made of. `agents` counts its own member
    identities; models / MCP servers / skills count what it can reach — its
    own bindings plus those inherited from ancestors while
    `inherit_from_parent` holds, enabled only (platform system MCP servers
    are not counted). Same numbers as the gallery card's resources."""

    agents: int = Field(0, description="Member agent identities (components)")
    models: int = Field(0, description="LLM models the system can reach (own + inherited, enabled)")
    mcp_servers: int = Field(0, description="MCP servers the system can reach (own + inherited, enabled; platform system servers excluded)")
    skills: int = Field(0, description="Agent skills the system can reach (own + inherited, enabled)")


class AISystemComposition(BaseModel):
    """The system's place in the composition: its members, what
    it depends on and what uses it."""

    members: AISystemMembers = Field(default_factory=AISystemMembers)
    depends_on: int = Field(0, description="Declared dependency edges")
    worst_dependency_health: Optional[
        Literal["healthy", "degraded", "idle", "unknown", "assumed"]
    ] = Field(None, description="Worst health across declared dependencies; null when none")
    used_by_declared: int = Field(
        0,
        description=(
            "Caller systems declared on either side: listed in this system's "
            "intended_client_systems, or declaring an edge to it"
        ),
    )
    used_by_observed: int = Field(
        0, description="Caller systems observed in run segments in the window"
    )
    idle: bool = Field(
        False,
        description="A shared service with no observed callers in the window — never a fault",
    )
    child_groups: int = Field(0, description="Direct child resource groups")


class AISystemLiveness(BaseModel):
    """The ONE liveness verdict — same value as the liveness
    panel and the health scorer."""

    state: Literal["alive", "silent", "idle", "unstarted", "not_configured"]
    reason: str


class AISystemTierSource(BaseModel):
    system_id: str
    system_name: Optional[str] = None
    tier: Optional[str] = None


class AISystemCardAgent(BaseModel):
    """Who the agent is: its registered identity, the implementation the
    latest run reported, and the outcome catalogue and contract it runs under."""

    identity_id: Optional[str] = Field(None, description="First bound agent identity (agent-<ULID>)")
    identity_name: Optional[str] = None
    identity_count: int = Field(0, description="Agent identities bound to the system")
    impl_name: Optional[str] = Field(None, description="Implementation name reported by the latest run")
    impl_version: Optional[str] = None
    impl_build: Optional[str] = None
    impl_trust: Optional[str] = Field(None, description="verified | declared")
    declared_outcomes: int = Field(0, description="Entries in the outcome catalogue")
    contract_version: Optional[int] = Field(None, description="Active contract version, else the latest")
    contract_status: Optional[str] = None


class AISystemCardGateway(BaseModel):
    """What the gateway enforces on this system's traffic. Counts are
    EFFECTIVE: bindings on the system and every ancestor (guardrails and
    policies are additive; limits restrictive, so a key set at two levels
    is one limit)."""

    policies: int = Field(0, description="Enabled argument + semantic + response policies and active policy bundles")
    guardrails: int = Field(0, description="Enabled guardrail configs bound on the chain")
    limits: int = Field(0, description="Distinct numeric limit settings in effect")
    blocks: int = Field(0, description="Guardrail blocks + policy denials in the window")


class AISystemCardResources(BaseModel):
    """What the system may reach — bindings on the system itself."""

    models: int = 0
    mcp_servers: int = 0
    skills: int = 0
    delegate_agents: int = Field(0, description="A2A agent cards the system may delegate to")


class AISystemCardKpis(BaseModel):
    """Observability and evaluation over the window. Every per-run figure
    is None when there is nothing to compute it over — 'no runs' and
    'zero' mean opposite things."""

    window_hours: int
    basis: str = Field(
        "runs",
        description=(
            "What the figures are computed over: 'runs' — the runs this system "
            "started; 'served_calls' — a shared service that started no run in "
            "the window: the calls it served inside other systems' runs (one "
            "segment each). On 'served_calls', traces counts "
            "served calls, latency is each call's active time and success uses "
            "the served-call classification."
        ),
    )
    traces: int = Field(0, description="Runs (one trace each), replays excluded; served calls on the 'served_calls' basis")
    spans: int = Field(0, description="Actions across those runs (LLM, tool, skill, A2A calls)")
    success_rate: Optional[float] = Field(None, description="success / (success + deviation + failed)")
    failed: int = 0
    p50_latency_ms: Optional[int] = None
    p95_latency_ms: Optional[int] = None
    cost_usd: float = 0.0
    tokens: int = Field(0, description="Total tokens across those runs (input + output)")
    input_tokens: int = Field(0, description="Input (prompt) tokens across those runs")
    output_tokens: int = Field(0, description="Output (completion) tokens across those runs")
    eval_suites: int = Field(0, description="Enabled replay (offline evaluation) suites")
    last_eval_pass_rate: Optional[float] = None
    last_eval_at: Optional[str] = None


class AISystemCardFacts(BaseModel):
    """The gallery card's extra facts — attached only with `include_card=true`."""

    agent: AISystemCardAgent
    gateway: AISystemCardGateway
    resources: AISystemCardResources
    kpis: AISystemCardKpis


class AISystemSummary(BaseModel):
    """One row on the fleet board."""

    id: str = Field(..., description="Resource group id — the AI System's identity")
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None

    system_kind: Optional[SystemKind] = Field(
        None,
        description=(
            "Shape of the system. Selects the lifecycle "
            "signal profile. Never null in practice — the model defaults it "
            "to 'agent' — but typed optional so a row without one "
            "cannot 500 the board."
        ),
    )

    # Governance attributes — these live on the RG.
    owner: Optional[str] = Field(None, description="Accountable owner (person or team)")
    intended_use: Optional[str] = Field(None, description="ISO 42001 intended-use documentation")
    intended_client_labels: Optional[List[str]] = Field(
        None,
        description=(
            "Declared client_label strings of non-system callers, compared "
            "against the client_label of the system's own runs."
        ),
    )
    intended_client_systems: Optional[List[str]] = Field(
        None,
        description=(
            "AI System ids this system accepts as callers, compared "
            "against the root systems of its run segments."
        ),
    )
    lifecycle_stage: Optional[LifecycleStage] = Field(
        None, description="proposed | approved | active | deprecated | retired"
    )
    eu_ai_act_risk_tier: Optional[EuAiActRiskTier] = Field(
        None, description="minimal | limited | high | unacceptable"
    )
    eu_ai_act_role: Optional[str] = Field(
        None,
        description=(
            "Regulated role this tenant holds for the system: "
            "provider | deployer | provider_and_deployer | importer | "
            "distributor | product_manufacturer | authorised_representative."
        ),
    )

    # Placement
    parent_group_id: Optional[str] = None
    hierarchy_level: int = 0
    hierarchy_path: Optional[str] = None
    is_active: bool = True

    composition: AISystemComposition = Field(default_factory=AISystemComposition)

    # Composite AI Systems.
    role: Literal["root", "shared_service", "root_and_service"] = Field(
        "root",
        description=(
            "shared_service = has at least one declared or observed caller "
            "system; root_and_service = also runs on its own or declares "
            "dependencies"
        ),
    )
    has_external_dependencies: bool = False
    risk_tier_effective: Optional[EuAiActRiskTier] = Field(
        None,
        description=(
            "max(declared tier, effective tier of every system declaring a "
            "contributing dependency on this one)"
        ),
    )
    effective_tier_sources: List[AISystemTierSource] = Field(
        default_factory=list,
        description="The callers that raised the effective tier above the declared one",
    )
    separability_declared: bool = Field(
        False,
        description=(
            "A contributes_to_decision=false edge with a separability "
            "rationale targets or originates from this system — same value "
            "as the Composition view's system.separability_declared"
        ),
    )
    separability_sealed: bool = Field(
        False,
        description=(
            "Every separability claim touching this system is "
            "sealed as a declaration record (false when there is none)"
        ),
    )
    enforce_accepted_callers: bool = Field(
        False,
        description=(
            "The gateway refuses A2A callers outside "
            "intended_client_systems (else unaccepted callers are an advisory finding)"
        ),
    )
    composition_finding_count: int = Field(
        0, description="Open composition findings"
    )
    liveness: Optional[AISystemLiveness] = None

    # Governance completeness — computed, not stored. The board's job before
    # the run ledger exists is to show operators what they have not filled in
    # yet, because an AI System with no owner and no intended use cannot be
    # assured later no matter how good the detectors are.
    governance_gaps: List[str] = Field(
        default_factory=list,
        description=(
            "Names of governance attributes that are unset on this system "
            "(e.g. 'owner', 'intended_use', 'lifecycle_stage', "
            "'eu_ai_act_risk_tier'). Empty means fully documented."
        ),
    )

    # Assurance rollup — populated only when the fleet is requested with
    # `include_health=true`, from the materialised ai_system_health rows
    # (one grouped query, never a per-system recompute). All null when the
    # system has never had health computed — which the UI must render as
    # `unknown`, never as healthy: absence of evidence is not green.
    health_state: Optional[str] = Field(
        None,
        description=(
            "healthy | learning | degraded | drifting | stalled | silent | "
            "suspended | unknown — worst-of across the five components. "
            "Null when include_health was not requested or never computed."
        ),
    )
    health_score: Optional[float] = Field(
        None, description="0–1 worst-of score; null when unknown/not requested"
    )
    open_drift_events: Optional[int] = Field(
        None, description="Open (unresolved) drift findings on this system"
    )
    health_computed_at: Optional[str] = Field(
        None, description="When the materialised health row was computed"
    )
    verdict_status: Optional[str] = Field(
        None,
        description=(
            "Pinned period verdict from the same health row: assured | "
            "assured_with_exceptions | violations | insufficient_evidence. "
            "The fleet board's row colour needs BOTH judgments — green is "
            "healthy AND assured, never one rounded up to the other."
        ),
    )

    #: Only set for `deprecated` systems. A deprecated system that
    #: has gone quiet is the one thing on this board that needs an action
    #: and never asks for one — without surfacing it, deprecated systems
    #: accumulate forever because nobody re-opens a page that said "not yet".
    retirable: Optional[bool] = Field(
        None,
        description=(
            "True when a deprecated system has been quiet long enough to "
            "retire. None for systems that are not deprecated."
        ),
    )
    retirable_at: Optional[str] = Field(
        None,
        description="When a deprecated system becomes retirable (ISO-8601).",
    )

    #: Gallery card facts — only with `include_card=true`.
    card: Optional[AISystemCardFacts] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AISystemFleetStats(BaseModel):
    """Aggregate counters across the returned fleet."""

    total: int = Field(0, description="AI Systems matching the filter")
    by_kind: dict = Field(default_factory=dict, description="system_kind -> count")
    by_lifecycle_stage: dict = Field(default_factory=dict, description="stage -> count")
    by_risk_tier: dict = Field(default_factory=dict, description="risk tier -> count")
    without_owner: int = Field(0, description="Systems with no accountable owner")
    without_intended_use: int = Field(0, description="Systems with no intended-use documentation")
    fully_documented: int = Field(0, description="Systems with no governance gaps")


class AISystemFleetResponse(BaseModel):
    """The fleet board payload."""

    systems: List[AISystemSummary]
    stats: AISystemFleetStats
    total: int
    limit: int
    offset: int
