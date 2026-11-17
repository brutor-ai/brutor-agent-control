# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems API models

Dependency edges (declare / edit / delete) and the Composition view. Every
field the service layer produces is declared here: Pydantic strips undeclared
fields, and a composition fact silently dropped from the response is a fact
the diagram never shows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

from app.api.v1.endpoints.admin._compliance_http import SealStatus
from app.services.ai_system_composition import DependencyAssumptions

DependencyRole = Literal["shared_service", "external"]
DependencyCriticality = Literal["critical", "degradable"]
DependencyHealth = Literal["healthy", "degraded", "idle", "unknown", "assumed"]
FindingSeverity = Literal["info", "warning", "critical"]
SystemRole = Literal["root", "shared_service", "root_and_service"]
PolicyKind = Literal["guardrail", "argument_policy", "response_policy", "governance", "cache"]
LimitKind = Literal["budget", "rate", "tokens", "run_cap", "envelope", "concurrency"]
ConnectionStatus = Literal["ok", "degraded", "down", "idle"]


# ---------------------------------------------------------------------------
# Dependency edges
# ---------------------------------------------------------------------------


class DependencyCreate(BaseModel):
    role: DependencyRole
    depends_on_system_id: Optional[str] = Field(
        None, description="Target AI System (required for shared_service)"
    )
    agent_card_id: Optional[str] = Field(
        None, description="Target agent card (required for external; optional for shared_service)"
    )
    criticality: DependencyCriticality = "critical"
    fallback: Optional[str] = Field(None, description="Required when degradable")
    contributes_to_decision: bool = True
    separability_rationale: Optional[str] = Field(
        None, description="Required when contributes_to_decision is false"
    )
    contract_pin: Optional[str] = Field(
        None,
        description="A contract id of the target; null = follow its active contract (shared_service only)",
    )
    supplier_legal_entity: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "The legal entity that provides the dependency when it is not the deployer — "
            "required for external; set on a shared service owned by another legal entity. "
            "The edge then needs supplier_information evidence on file (EU AI Act Art 25(4))."
        ),
    )
    assumptions: DependencyAssumptions = Field(
        default_factory=DependencyAssumptions,
        description="The root's side of an assume-guarantee contract (strict schema; unknown keys → 422)",
    )
    expected_outcomes: Optional[List[str]] = Field(
        None,
        description=(
            "The delegate outcomes (<outcome>[.<variant>]) this system is "
            "built to handle from the dependency. An answer outside the list is an "
            "unexpected_delegate_outcome finding. null = not declared (no finding)."
        ),
    )


class DependencyUpdate(BaseModel):
    """PATCH: omitted fields are unchanged; the merged edge is re-validated."""

    model_config = ConfigDict(extra="forbid")

    role: Optional[DependencyRole] = None
    depends_on_system_id: Optional[str] = None
    agent_card_id: Optional[str] = None
    criticality: Optional[DependencyCriticality] = None
    fallback: Optional[str] = None
    contributes_to_decision: Optional[bool] = None
    separability_rationale: Optional[str] = None
    contract_pin: Optional[str] = None
    supplier_legal_entity: Optional[str] = Field(None, max_length=255)
    assumptions: Optional[DependencyAssumptions] = None
    expected_outcomes: Optional[List[str]] = None


class DependencyEdge(BaseModel):
    id: str
    ai_system_id: str
    role: DependencyRole
    depends_on_system_id: Optional[str] = None
    depends_on_system_name: Optional[str] = None
    agent_card_id: Optional[str] = None
    agent_card_name: Optional[str] = None
    criticality: DependencyCriticality
    fallback: Optional[str] = None
    contributes_to_decision: bool
    separability_rationale: Optional[str] = None
    separability_record_id: Optional[str] = Field(
        None,
        description="The sealed ai.brutor.declaration of the separability claim (null = not sealed)",
    )
    separability_sealed_at: Optional[str] = None
    contract_pin: Optional[str] = None
    supplier_legal_entity: Optional[str] = None
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    expected_outcomes: Optional[List[str]] = None
    declared_by: Optional[str] = None
    declared_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    separability_seal: Optional[SealStatus] = Field(
        None,
        description=(
            "Write responses only: the outcome of sealing the separability claim on THIS "
            "save (null when the save sealed nothing)"
        ),
    )


class DependencyListResponse(BaseModel):
    dependencies: List[DependencyEdge]
    total: int


# ---------------------------------------------------------------------------
# Composition view
# ---------------------------------------------------------------------------


class InheritedFromRef(BaseModel):
    id: str
    name: Optional[str] = None


class ContractRef(BaseModel):
    id: str
    version: int


class TargetContractRef(ContractRef):
    pinned: bool


class TierSource(BaseModel):
    system_id: str
    system_name: Optional[str] = None
    tier: Optional[str] = None


class SeparabilityDeclaration(BaseModel):
    """A non-contributing edge's separability claim, shown on BOTH systems
    with the sealed declaration record."""

    edge_id: str
    ai_system_id: str
    ai_system_name: Optional[str] = None
    depends_on_system_id: Optional[str] = None
    depends_on_system_name: Optional[str] = None
    agent_card_id: Optional[str] = None
    agent_card_name: Optional[str] = None
    side: Literal["declares", "declared_on"] = Field(
        ..., description="declares = this system's own edge; declared_on = a caller's edge to this system"
    )
    separability_rationale: Optional[str] = None
    record_id: Optional[str] = None
    sealed_at: Optional[str] = None
    sealed: bool = False


class CompositionSystem(BaseModel):
    id: str
    name: str
    system_kind: Optional[str] = Field(
        None, description="The AI System's kind (agent, assistant, workflow, …)"
    )
    owner: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    autonomy_level: Optional[str] = None
    active_contract: Optional[ContractRef] = None
    risk_tier_declared: Optional[str] = None
    risk_tier_effective: Optional[str] = None
    effective_tier_sources: List[TierSource] = Field(default_factory=list)
    separability_declared: bool = False
    separability_declarations: List[SeparabilityDeclaration] = Field(default_factory=list)
    enforce_accepted_callers: bool = Field(
        False,
        description="The gateway refuses A2A callers not in intended_client_systems",
    )


class LocationEntry(BaseModel):
    id: str
    name: Optional[str] = None
    group_type: Optional[str] = None


class AgentReleaseRef(BaseModel):
    """A member agent's CURRENT release (the one seen last)."""

    name: str
    version: str
    build: Optional[str] = None
    trust: Literal["verified", "declared"]
    approved: Optional[bool] = Field(
        None, description="Derived from approved_versions; null = the identity approves nothing (observe only)"
    )
    last_seen_at: Optional[str] = None


class AgentRef(BaseModel):
    id: str
    name: Optional[str] = None
    implementation_name: Optional[str] = None
    approved_versions: List[str] = Field(default_factory=list)
    require_release: bool = False
    release: Optional[AgentReleaseRef] = Field(
        None, description="The member agent's current release; null when it has never declared one"
    )


class CompositionConnection(BaseModel):
    """One connector line of the Simple graph — THE rule is
    `ai_system_connection_status.connection_status` (thresholds there)."""

    status: ConnectionStatus = Field(
        ...,
        description=(
            "ok = calls in the window, nothing wrong; degraded = error+block rate >= 5%, "
            "a degraded health signal, or a hold for approval that expired without a "
            "decision; down = a down health signal (disabled, retired, unhealthy, circuit "
            "open, not answering) or the last 3 calls all failed or were blocked; idle = no "
            "calls in the window and no negative health signal. Holds for approval are "
            "counted apart (held) and are never a failure"
        ),
    )
    reason: str = Field(..., description="Why, in words (the line's tooltip)")
    calls: int = 0
    errors: int = 0
    blocked: int = Field(0, description="Calls the gateway refused (holds not included)")
    held: int = Field(0, description="Calls held for a human approval decision")
    held_lapsed: int = Field(
        0,
        description="Of held: no decision before the approval timeout (expired) — a warning",
    )
    last_call_at: Optional[str] = None


class ResourceRef(BaseModel):
    id: str
    name: Optional[str] = None
    inherited_from: Optional[InheritedFromRef] = None
    connection: CompositionConnection = Field(
        ..., description="This system's own connection to the resource in the window"
    )


class McpResourceRef(ResourceRef):
    """An MCP server binding. `system` marks the platform's own system MCP
    servers (e.g. Agent Skills) — gateway plumbing a view may leave out."""

    system: bool = Field(
        False,
        description=(
            "A platform-managed system MCP server (base_url under http://system/), "
            "not a server the system's owner connected"
        ),
    )
    mcp_server_id: Optional[str] = Field(
        None,
        description=(
            "The mcp_servers id behind this binding (`id` is the tenant's "
            "server_config) — what a proxy-log row's server_id names, so a "
            "run's MCP actions can be placed on this binding"
        ),
    )


class CompositionResources(BaseModel):
    models: List[ResourceRef] = Field(default_factory=list)
    mcp_servers: List[McpResourceRef] = Field(default_factory=list)
    skills: List[ResourceRef] = Field(default_factory=list)
    knowledge_bases: List[ResourceRef] = Field(default_factory=list)


class GatewayPolicy(BaseModel):
    kind: PolicyKind
    id: Optional[str] = None
    name: str
    summary: Optional[str] = None
    inherited_from: Optional[InheritedFromRef] = None


class GatewayLimit(BaseModel):
    kind: LimitKind
    key: str
    value: Any = None
    summary: Optional[str] = None
    inherited_from: Optional[InheritedFromRef] = None


class CompositionGateway(BaseModel):
    policies: List[GatewayPolicy] = Field(default_factory=list)
    limits: List[GatewayLimit] = Field(default_factory=list)


class ObservedCalls(BaseModel):
    """Segment statistics for one edge in the window."""

    calls: int = 0
    answered: int = 0
    failed: int = 0
    rejected: int = 0
    timed_out: int = 0
    blocked: int = 0
    last_seen_at: Optional[str] = None


class CompositionDependency(BaseModel):
    """A delegation node: a declared edge, or a card an a2a_call grant
    reaches with no edge (`declared: false`, edge fields null)."""

    id: Optional[str] = None
    ai_system_id: str
    role: Optional[DependencyRole] = None
    depends_on_system_id: Optional[str] = None
    depends_on_system_name: Optional[str] = None
    agent_card_id: Optional[str] = None
    agent_card_name: Optional[str] = None
    criticality: Optional[DependencyCriticality] = None
    fallback: Optional[str] = None
    contributes_to_decision: Optional[bool] = None
    separability_rationale: Optional[str] = None
    separability_record_id: Optional[str] = None
    separability_sealed_at: Optional[str] = None
    contract_pin: Optional[str] = None
    supplier_legal_entity: Optional[str] = None
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    declared_by: Optional[str] = None
    declared_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    declared: bool = Field(True, description="A dependency edge is declared for this node")
    granted: bool = Field(
        False, description="An enabled, non-deny a2a_call grant of a member agent names this card"
    )
    health: DependencyHealth
    observed: ObservedCalls
    target_contract: Optional[TargetContractRef] = None
    connection: CompositionConnection = Field(
        ..., description="This system's connection to the delegation agent in the window"
    )


class ObservedUndeclared(BaseModel):
    system_id: str
    system_name: Optional[str] = None
    calls: int
    last_seen_at: Optional[str] = None
    connection: CompositionConnection


class Dependent(BaseModel):
    system_id: str
    system_name: Optional[str] = None
    declared: bool = Field(
        ..., description="Accepted by this system (listed in its intended_client_systems)"
    )
    edge_declared: bool = Field(
        False, description="The caller declares a dependency edge to this system"
    )
    observed_calls: int = 0
    last_seen_at: Optional[str] = None


class ClientLabel(BaseModel):
    label: str
    declared: bool
    observed_calls: int = 0


class CompositionFinding(BaseModel):
    code: str
    severity: FindingSeverity
    message: str
    subject_id: Optional[str] = None


class FallbackCheck(BaseModel):
    """Did the root complete the runs in which a degradable dependency failed?"""

    unanswered_calls: int = 0
    root_runs_completed: int = 0
    held: Optional[bool] = Field(
        None, description="null = not exercised in the window; false = a run did not complete"
    )


class BoundaryMetrics(BaseModel):
    """An external dependency, seen only at the gateway boundary."""

    calls: int = Field(0, description="Every call in the window (a fact)")
    excluded_calls: int = Field(
        0,
        description=(
            "Calls whose run lies inside an incident window: counted in "
            "`calls`, left out of errors, error_rate, p95 and task_states"
        ),
    )
    errors: int = Field(0, description="Errors among the judged calls")
    error_rate: Optional[float] = None
    p95_latency_ms: Optional[int] = None
    last_seen_at: Optional[str] = None
    task_states: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Terminal A2A task states the root received "
            "(proxy_logs.a2a_task_state: completed, failed, canceled, rejected, "
            "input_required, auth_required, …) → calls"
        ),
    )


class DependencyHealthEntry(BaseModel):
    """One declared dependency's health, as the root sees it."""

    edge_id: str
    system_id: Optional[str] = None
    name: Optional[str] = None
    role: DependencyRole
    criticality: DependencyCriticality
    contributes_to_decision: bool = True
    status: DependencyHealth
    failing_signal: Optional[str] = Field(
        None,
        description=(
            "liveness | autonomy | reliability | behaviour | cost | conformance | "
            "oversight | review | dependency (a critical dependency of the "
            "dependency fails — see via_system_id)"
        ),
    )
    via_system_id: Optional[str] = None
    via_system_name: Optional[str] = None
    basis: Literal["tested", "assumed"]
    caps_root: bool = Field(
        False, description="A critical shared service that is not healthy: the root is capped at degraded"
    )
    note: Optional[str] = None
    fallback_check: Optional[FallbackCheck] = None
    boundary: Optional[BoundaryMetrics] = None
    assumptions: Optional[Dict[str, Any]] = None


class DependencyCap(BaseModel):
    """Why the root's health is capped at degraded."""

    reason: Literal["dependency"]
    edge_id: str
    system_id: Optional[str] = None
    name: Optional[str] = None
    failing_signal: Optional[str] = None


class HumanApprovalGate(BaseModel):
    source: str = Field(
        ...,
        description="agent_grant | mcp_capability | argument_policy | skill_execution_mode | skill_default",
    )
    scope: str = Field(
        ..., description="always | conditional (only when the arguments match a rule)"
    )
    lane: str = Field(..., description="mcp | a2a | skills")
    target: str = Field(..., description="The tool, card or skill it names; may be a glob")
    resource_ids: List[str] = Field(
        default_factory=list,
        description=(
            "The bound resources it applies to. Empty = a real gate that names "
            "nothing a bound resource reports: shown on the lane, never dropped"
        ),
    )
    detail: str


class HumanApproval(BaseModel):
    """Where the system is declared to wait for a person, from the closure.
    What actually happened is on the connection lines (`held`, `held_lapsed`)."""

    autonomy_requires_approval: bool = Field(
        False, description="Side-effecting agent calls wait for a person, on every resource"
    )
    approval_window_seconds: Optional[int] = Field(
        None, description="How long a person has to decide, when the system's chain states it"
    )
    gates: List[HumanApprovalGate] = Field(default_factory=list)


class CompositionResponse(BaseModel):
    system: CompositionSystem
    window_hours: int
    location: List[LocationEntry]
    agents: List[AgentRef]
    resources: CompositionResources
    system_status: CompositionConnection = Field(
        ..., description="The system → gateway line, from the system's stored health"
    )
    gateway: CompositionGateway
    human_approval: HumanApproval = Field(default_factory=HumanApproval)
    dependencies: List[CompositionDependency]
    observed_undeclared: List[ObservedUndeclared]
    dependents: List[Dependent]
    client_labels: List[ClientLabel]
    findings: List[CompositionFinding]
