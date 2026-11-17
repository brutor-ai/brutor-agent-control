# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems

An AI System is a composite: its own member agents (components), the
resources the gateway lets it reach, and the other AI Systems or external
agents it delegates to. This module owns the composition facts every surface
reads:

  * **Dependency edges** (`ai_system_dependencies`) — validation of a
    declared edge, one rule set for create and update.
  * **The composition index** — one tenant-wide pass over AI Systems, edges
    and run segments that yields, per system: declared vs observed
    dependencies and dependents, role, effective risk tier and composition
    findings. The AI Systems list and the Composition endpoint both read it,
    so the list's finding count and the tab's finding list are the same
    computation.
  * **The Composition view** — built on `resolve_closure` (the contract's
    own computation: what an approver signs is what an operator sees), plus
    the index.

## Attribution of steps to systems

A step (proxy_logs row) belongs to the nearest AI System up its resource
group's parent chain — the rule the core run ledger uses to cut segments.
`nearest_ai_system_map` is the control plane's copy of that walk, used where
the control plane reads steps directly (conformance's "models used").

## Card → system resolution

An `a2a_call` grant names an agent card, never the AI System behind it, and
nothing in the schema links the two. A card resolves to a system only
through a declared `shared_service` edge that pairs them — anywhere in the
tenant. That is what `role_mismatch` checks: an edge declared `external`
whose card another edge has already identified as an AI System here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.database.models.agent_card import AgentCard
from app.database.models.ai_system_contract import AISystemContract, CONTRACT_ACTIVE
from app.database.models.ai_system_dependency import (
    CRITICALITY_DEGRADABLE,
    DEPENDENCY_CRITICALITIES,
    DEPENDENCY_ROLES,
    ROLE_EXTERNAL,
    ROLE_SHARED_SERVICE,
    AISystemDependency,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import (
    SEGMENT_OUTCOMES,
    AISystemRunSegment,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_connection_status import (
    attach_connections,
    held_segment_expr,
    lapsed_held_segment_expr,
)
from app.services.ai_system_down_rule import (
    CALLS_DOWN,
    CALLS_OK,
    DOWN_AFTER_CONSECUTIVE_FAILURES,
    SEGMENT_REFUSED,
    segment_call_outcome_sql,
)
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_log, judged_segment
from app.services.ai_system_liveness_state import segment_calls_verdict

DEFAULT_COMPOSITION_WINDOW_HOURS = 168

#: Mirrors the closure's hierarchy bound (and the core's).
MAX_HIERARCHY_DEPTH = 10

# ---------------------------------------------------------------------------
# Vocabulary — wire contract with the Admin UI (pinned in tests/test_wire_contract.py)
# ---------------------------------------------------------------------------

ROLE_ROOT = "root"
ROLE_SERVICE = "shared_service"
ROLE_ROOT_AND_SERVICE = "root_and_service"
SYSTEM_ROLES = (ROLE_ROOT, ROLE_SERVICE, ROLE_ROOT_AND_SERVICE)

ROLE_FILTER_ROOT = "root"
ROLE_FILTER_SHARED_SERVICE = "shared_service"
ROLE_FILTER_EXTERNAL_DEPS = "external_deps"
ROLE_FILTERS = (ROLE_FILTER_ROOT, ROLE_FILTER_SHARED_SERVICE, ROLE_FILTER_EXTERNAL_DEPS)

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_IDLE = "idle"
HEALTH_UNKNOWN = "unknown"
HEALTH_ASSUMED = "assumed"
DEPENDENCY_HEALTHS = (HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_IDLE, HEALTH_UNKNOWN, HEALTH_ASSUMED)
#: Worst first. `unknown` outranks `assumed`: an undeclared card is a gap in
#: what the owner has said, an external edge is a stated carve-out.
_HEALTH_RANK = {
    HEALTH_DEGRADED: 4,
    HEALTH_UNKNOWN: 3,
    HEALTH_ASSUMED: 2,
    HEALTH_IDLE: 1,
    HEALTH_HEALTHY: 0,
}

FINDING_UNDECLARED_DEPENDENCY = "composition.undeclared_dependency"
FINDING_UNACCEPTED_CALLER = "composition.unaccepted_caller"
FINDING_DECLARED_NEVER_OBSERVED = "composition.declared_never_observed"
FINDING_ROLE_MISMATCH = "composition.role_mismatch"
FINDING_DEPENDENCY_CYCLE = "composition.dependency_cycle"
# Grants bound to declarations, and pins.
FINDING_GRANT_WITHOUT_EDGE = "composition.grant_without_edge"
FINDING_EDGE_WITHOUT_GRANT = "composition.edge_without_grant"
FINDING_DEPENDENCY_PIN_STALE = "composition.dependency_pin_stale"
COMPOSITION_FINDING_CODES = (
    FINDING_UNDECLARED_DEPENDENCY,
    FINDING_UNACCEPTED_CALLER,
    FINDING_DECLARED_NEVER_OBSERVED,
    FINDING_ROLE_MISMATCH,
    FINDING_DEPENDENCY_CYCLE,
    FINDING_GRANT_WITHOUT_EDGE,
    FINDING_EDGE_WITHOUT_GRANT,
    FINDING_DEPENDENCY_PIN_STALE,
)
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: EU AI Act tier order (resource_groups.eu_ai_act_risk_tier — the source of
#: truth the compliance profile reads and writes back).
RISK_TIER_ORDER = ("minimal", "limited", "high", "unacceptable")
_TIER_RANK = {t: i for i, t in enumerate(RISK_TIER_ORDER)}


def tier_rank(tier: Optional[str]) -> int:
    return _TIER_RANK.get(tier or "", -1)


def max_tier(*tiers: Optional[str]) -> Optional[str]:
    """The highest of `tiers` in RISK_TIER_ORDER (None when none is known)."""
    best: Optional[str] = None
    for t in tiers:
        if tier_rank(t) > tier_rank(best):
            best = t
    return best


def propagate_effective_tiers(
    declared: Dict[str, Optional[str]], edges: Iterable[AISystemDependency]
) -> Dict[str, Optional[str]]:
    """Effective tier = max(own declared, effective tier of
    every system that declares a CONTRIBUTING shared-service edge to it),
    propagated to a fixed point so a chain A → B → C carries A's tier to C.
    Monotone max over a finite order, so it terminates, cycles included.

    THE effective-tier computation: the composition index (list, tab) and
    the obligation registry (`load_tier_facts`) both call it.
    """
    eff = dict(declared)
    contributing = [
        (e.ai_system_id, e.depends_on_system_id)
        for e in edges
        if e.role == ROLE_SHARED_SERVICE
        and e.contributes_to_decision
        and e.depends_on_system_id in eff
        and e.ai_system_id in eff
    ]
    changed = True
    while changed:
        changed = False
        for caller, target in contributing:
            if tier_rank(eff[caller]) > tier_rank(eff[target]):
                eff[target] = eff[caller]
                changed = True
    return eff


def effective_tier_sources_of(
    sid: str,
    declared: Dict[str, Optional[str]],
    effective: Dict[str, Optional[str]],
    edges: Iterable[AISystemDependency],
    names: Dict[str, str],
) -> List[Dict[str, Any]]:
    """The callers that raised `sid`'s tier above its declared one."""
    eff = effective.get(sid)
    if tier_rank(eff) <= tier_rank(declared.get(sid)):
        return []
    out = [
        {
            "system_id": e.ai_system_id,
            "system_name": names.get(e.ai_system_id),
            "tier": effective.get(e.ai_system_id),
        }
        for e in edges
        if e.role == ROLE_SHARED_SERVICE
        and e.contributes_to_decision
        and e.depends_on_system_id == sid
        and tier_rank(effective.get(e.ai_system_id)) == tier_rank(eff)
    ]
    return sorted(out, key=lambda s: s["system_id"])


@dataclass
class TierFacts:
    """A tenant's declared and effective risk tiers plus its edges — the
    light load the obligation registry needs (no segments, no logs)."""

    declared: Dict[str, Optional[str]]
    effective: Dict[str, Optional[str]]
    names: Dict[str, str]
    edges: List[AISystemDependency]

    def effective_tier(self, sid: str) -> Optional[str]:
        return self.effective.get(sid)

    def sources(self, sid: str) -> List[Dict[str, Any]]:
        return effective_tier_sources_of(sid, self.declared, self.effective, self.edges, self.names)

    def edges_of(self, sid: str) -> List[AISystemDependency]:
        return [e for e in self.edges if e.ai_system_id == sid]


def load_tier_facts(db: Session, tenant_id: str) -> TierFacts:
    declared: Dict[str, Optional[str]] = {}
    names: Dict[str, str] = {}
    for gid, name, display, tier in db.query(
        ResourceGroup.id, ResourceGroup.name, ResourceGroup.display_name,
        ResourceGroup.eu_ai_act_risk_tier,
    ).filter(
        ResourceGroup.tenant_id == tenant_id,
        ResourceGroup.group_type == GroupType.AI_SYSTEM,
    ):
        declared[gid] = tier
        names[gid] = display or name
    edges = (
        db.query(AISystemDependency)
        .filter(AISystemDependency.tenant_id == tenant_id)
        .order_by(AISystemDependency.ai_system_id, AISystemDependency.declared_at, AISystemDependency.id)
        .all()
    )
    return TierFacts(
        declared=declared,
        effective=propagate_effective_tiers(declared, edges),
        names=names,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def is_ai_system(group: Optional[ResourceGroup]) -> bool:
    return group is not None and group.group_type == GroupType.AI_SYSTEM


def load_ai_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
    """The AI System, or `LookupError` (absent) / `ValueError` (not an AI System)."""
    group = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.id == ai_system_id, ResourceGroup.tenant_id == tenant_id)
        .first()
    )
    if group is None:
        raise LookupError(f"AI System {ai_system_id!r} not found")
    if not is_ai_system(group):
        raise ValueError(f"Resource group {ai_system_id!r} is not an AI System")
    return group


def nearest_ai_system_map(db: Session, tenant_id: str) -> Dict[str, Optional[str]]:
    """`{resource group id: nearest AI System id at-or-above it | None}`.

    The run ledger's attribution rule, as a lookup table. One query for the
    tenant's groups; the walk is bounded like every other hierarchy walk.
    """
    rows = (
        db.query(ResourceGroup.id, ResourceGroup.parent_group_id, ResourceGroup.group_type)
        .filter(ResourceGroup.tenant_id == tenant_id)
        .all()
    )
    parent = {gid: pid for gid, pid, _ in rows}
    is_system = {gid for gid, _, gt in rows if gt == GroupType.AI_SYSTEM}
    out: Dict[str, Optional[str]] = {}
    for gid in parent:
        current: Optional[str] = gid
        found: Optional[str] = None
        for _ in range(MAX_HIERARCHY_DEPTH + 1):
            if current is None:
                break
            if current in is_system:
                found = current
                break
            current = parent.get(current)
        out[gid] = found
    return out


# ---------------------------------------------------------------------------
# Dependency validation (one rule set for create and update)
# ---------------------------------------------------------------------------


class DependencyValidationError(ValueError):
    """→ 422. The message names the rule."""


#: What a dependency may be assumed to provide as evidence of its own
#:. Wire contract with the Admin UI.
ASSUMPTION_EVIDENCE_KINDS = (
    "signed_agent_card", "attestation", "certification", "contract", "other",
)


class DependencyAssumptions(BaseModel):
    """The root's side of an assume-guarantee contract: what
    it ASSUMES about a dependency it cannot test. Strict — an unknown key is
    a 422, never silently stored, because an assumption nobody can read back
    under a known name is not a stated assumption.

    Every field is optional; an empty object states nothing.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    data_classes_shared: Optional[List[str]] = Field(
        None, max_length=50,
        description="Data classes the root shares with the dependency (e.g. 'personal_data', 'financial')",
    )
    retention_at_delegate: Optional[str] = Field(
        None, max_length=500, description="What the delegate is assumed to retain, and for how long",
    )
    human_oversight_at_delegate: Optional[bool] = Field(
        None, description="Whether a human oversees the delegate's decisions on its side",
    )
    expected_availability_pct: Optional[float] = Field(
        None, ge=0, le=100, description="Availability the root assumes (0–100)",
    )
    expected_p95_latency_ms: Optional[int] = Field(
        None, ge=0, description="p95 latency the root assumes, in milliseconds",
    )
    evidence_provided: Optional[
        List[Literal["signed_agent_card", "attestation", "certification", "contract", "other"]]
    ] = Field(None, max_length=10, description="Evidence the dependency provides of its own")


def validate_assumptions(value: Any) -> Dict[str, Any]:
    """The normalised assumptions object (unset keys omitted), or
    `DependencyValidationError` naming the first offending field."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DependencyValidationError("assumptions must be an object")
    data = dict(value)
    # JSON has one number type; an integral availability is still a number.
    if isinstance(data.get("expected_availability_pct"), int) and not isinstance(
        data.get("expected_availability_pct"), bool
    ):
        data["expected_availability_pct"] = float(data["expected_availability_pct"])
    try:
        parsed = DependencyAssumptions.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        raise DependencyValidationError(
            f"assumptions{'.' + loc if loc else ''}: {err.get('msg')}"
        ) from exc
    return parsed.model_dump(exclude_none=True)


class DependencyConflictError(ValueError):
    """→ 409. An equivalent edge already exists."""


def validate_dependency(
    db: Session,
    *,
    tenant_id: str,
    ai_system_id: str,
    fields: Dict[str, Any],
    edge_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate the RESULTING edge (after a PATCH merge) and normalise it.

    Every rule is checked against the merged state because each field's
    validity depends on others: `fallback` is required only when
    `degradable`, `separability_rationale` only when not contributing, a
    `contract_pin` only means something on a `shared_service`.
    """
    role = fields.get("role")
    if role not in DEPENDENCY_ROLES:
        raise DependencyValidationError(f"role must be one of {list(DEPENDENCY_ROLES)}")

    criticality = fields.get("criticality") or "critical"
    if criticality not in DEPENDENCY_CRITICALITIES:
        raise DependencyValidationError(
            f"criticality must be one of {list(DEPENDENCY_CRITICALITIES)}"
        )
    fields["criticality"] = criticality

    target_id = fields.get("depends_on_system_id") or None
    card_id = fields.get("agent_card_id") or None
    fields["depends_on_system_id"] = target_id
    fields["agent_card_id"] = card_id

    if target_id is not None and target_id == ai_system_id:
        raise DependencyValidationError("An AI System cannot depend on itself")

    if role == ROLE_SHARED_SERVICE:
        if target_id is None:
            raise DependencyValidationError(
                "A shared_service dependency requires depends_on_system_id"
            )
        target = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.id == target_id, ResourceGroup.tenant_id == tenant_id)
            .first()
        )
        if not is_ai_system(target):
            raise DependencyValidationError(
                f"depends_on_system_id {target_id!r} is not an AI System in this tenant"
            )
    else:  # external
        if card_id is None:
            raise DependencyValidationError("An external dependency requires agent_card_id")
        if target_id is not None:
            raise DependencyValidationError(
                "An external dependency has no Brutor AI System behind it — "
                "declare it as a shared_service to name depends_on_system_id"
            )
        if fields.get("contract_pin"):
            raise DependencyValidationError(
                "contract_pin applies to shared_service dependencies only"
            )

    if card_id is not None:
        card = (
            db.query(AgentCard.id)
            .filter(AgentCard.id == card_id, AgentCard.tenant_id == tenant_id)
            .first()
        )
        if card is None:
            raise DependencyValidationError(
                f"agent_card_id {card_id!r} is not an agent card in this tenant"
            )

    fallback = (fields.get("fallback") or "").strip() or None
    fields["fallback"] = fallback
    if criticality == CRITICALITY_DEGRADABLE and not fallback:
        raise DependencyValidationError(
            "A degradable dependency requires a fallback: what the system does "
            "when the dependency fails"
        )

    contributes = fields.get("contributes_to_decision")
    contributes = True if contributes is None else bool(contributes)
    fields["contributes_to_decision"] = contributes
    rationale = (fields.get("separability_rationale") or "").strip() or None
    fields["separability_rationale"] = rationale
    if not contributes and not rationale:
        raise DependencyValidationError(
            "contributes_to_decision=false requires a separability_rationale"
        )

    # The delegate outcomes the caller handles.
    from app.services.ai_system_outcome_catalogue import validate_expected_outcomes

    try:
        fields["expected_outcomes"] = validate_expected_outcomes(fields.get("expected_outcomes"))
    except ValueError as exc:
        raise DependencyValidationError(str(exc)) from exc

    pin = fields.get("contract_pin") or None
    fields["contract_pin"] = pin
    if pin is not None:
        exists = (
            db.query(AISystemContract.id)
            .filter(
                AISystemContract.id == pin,
                AISystemContract.tenant_id == tenant_id,
                AISystemContract.ai_system_id == target_id,
            )
            .first()
        )
        if exists is None:
            raise DependencyValidationError(
                f"contract_pin {pin!r} is not a contract of {target_id!r}"
            )

    fields["assumptions"] = validate_assumptions(fields.get("assumptions"))

    # Who supplies the dependency (EU AI Act Art 25(4)). An external
    # agent is by definition supplied by someone other than the deployer, so
    # naming them is required; a shared service names one only when another
    # legal entity owns it. Either way the edge then needs supplier
    # information on file (the obligation registry checks it).
    supplier = (fields.get("supplier_legal_entity") or "").strip() or None
    if supplier is not None and len(supplier) > 255:
        raise DependencyValidationError("supplier_legal_entity is at most 255 characters")
    fields["supplier_legal_entity"] = supplier
    if role == ROLE_EXTERNAL and supplier is None:
        raise DependencyValidationError(
            "An external dependency requires supplier_legal_entity: the legal "
            "entity that provides the agent (EU AI Act Art 25(4))"
        )

    # Duplicates — checked here for a clean 409 (the unique constraints are
    # the backstop under concurrency).
    dup_q = db.query(AISystemDependency.id).filter(
        AISystemDependency.tenant_id == tenant_id,
        AISystemDependency.ai_system_id == ai_system_id,
    )
    if edge_id is not None:
        dup_q = dup_q.filter(AISystemDependency.id != edge_id)
    if target_id is not None and dup_q.filter(
        AISystemDependency.depends_on_system_id == target_id
    ).first():
        raise DependencyConflictError(
            f"A dependency on {target_id!r} is already declared for this system"
        )
    if card_id is not None and dup_q.filter(
        AISystemDependency.agent_card_id == card_id
    ).first():
        raise DependencyConflictError(
            f"A dependency on agent card {card_id!r} is already declared for this system"
        )
    return fields


def validate_intended_client_systems(
    db: Session, *, tenant_id: str, group_id: Optional[str], ids: Optional[List[str]]
) -> Optional[List[str]]:
    """Normalise `intended_client_systems`: AI Systems in the tenant, not self.

    Returns a de-duplicated, sorted list (None stays None). Raises
    `DependencyValidationError` naming the offending id.
    """
    if ids is None:
        return None
    clean = sorted({i for i in ids if i})
    if group_id is not None and group_id in clean:
        raise DependencyValidationError("An AI System cannot be its own intended client")
    if clean:
        found = {
            gid
            for (gid,) in db.query(ResourceGroup.id).filter(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.id.in_(clean),
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
            )
        }
        missing = [i for i in clean if i not in found]
        if missing:
            raise DependencyValidationError(
                f"intended_client_systems must be AI Systems in this tenant: {missing}"
            )
    return clean


# ---------------------------------------------------------------------------
# The composition index — one tenant-wide computation
# ---------------------------------------------------------------------------


@dataclass
class PairStats:
    """Segments of `segment` entered by a call from `caller` (the immediate
    calling AI System)."""

    #: FACTS (windows excuse performance, never
    #: facts): every call in the window, in-window or not. What the
    #: composition checks read (an undeclared dependency seen only during an
    #: outage is still undeclared) and what the tab shows as observed.
    calls: int = 0
    by_outcome: Dict[str, int] = field(default_factory=dict)
    last_seen_at: Optional[datetime] = None
    #: The delegate's reported outcome ids ("" = none), with
    #: counts: what `unexpected_delegate_outcome` checks against the caller
    #: dependency's `expected_outcomes`.
    outcome_ids: Dict[str, int] = field(default_factory=dict)
    #: RATES: the calls outside every incident window — what a connection
    #: line's failure share is computed from.
    judged_calls: int = 0
    judged_by_outcome: Dict[str, int] = field(default_factory=dict)
    latest_outcome: Optional[str] = None
    #: The latest JUDGED outcomes, newest first — `(last_action_at,
    #: outcome)`, at most `DOWN_AFTER_CONSECUTIVE_FAILURES` (THE down rule,
    #: `ai_system_down_rule`).
    recent: List[Tuple[datetime, str]] = field(default_factory=list)
    #: Of the judged blocked calls, those HELD for a person (counted
    #: apart by the connection line), and of those, the ones nobody decided
    #: before the approval timeout. `recent_held` flags `recent` entry by
    #: entry. Every other reader still sees a held call as `blocked`.
    judged_held: int = 0
    judged_held_lapsed: int = 0
    recent_held: List[bool] = field(default_factory=list)

    def calls_verdict(self) -> Optional[str]:
        """ok | degraded | down | None — THE down rule over this caller's
        latest calls into the segment system."""
        return segment_calls_verdict(o for _, o in self.recent)

    def observed(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"calls": self.calls}
        for outcome in (*SEGMENT_OUTCOMES, SEGMENT_REFUSED):
            out[outcome] = self.by_outcome.get(outcome, 0)
        out["last_seen_at"] = _iso(self.last_seen_at)
        return out


_EMPTY = PairStats()


#: The failing signal a dependency's own responsiveness names: all of its
#: latest calls unanswered is an outage (`liveness`); some is `reliability`.
FAILING_SIGNAL_NOT_ANSWERING = "liveness"
FAILING_SIGNAL_SOME_UNANSWERED = "reliability"


def edge_health(edge: Optional[AISystemDependency], stats: Optional[PairStats]) -> str:
    """healthy | degraded | idle | unknown | assumed for one dependency.

    `external` edges are never tested (assumed); an undeclared node is
    unknown. A shared service's health is its responsiveness to THIS caller,
    by THE down rule the liveness function applies to all of its callers
    (`ai_system_down_rule`): an unanswered call among the latest degrades
    it. The health vocabulary has no "down" — `dependency_status` names
    the outage as failing signal `liveness`.
    """
    if edge is None:
        return HEALTH_UNKNOWN
    if edge.role == ROLE_EXTERNAL:
        return HEALTH_ASSUMED
    verdict = stats.calls_verdict() if stats else None
    if verdict is None:
        return HEALTH_IDLE
    return HEALTH_HEALTHY if verdict == CALLS_OK else HEALTH_DEGRADED


def worst_health(healths: Iterable[str]) -> Optional[str]:
    healths = list(healths)
    if not healths:
        return None
    return max(healths, key=lambda h: _HEALTH_RANK.get(h, 0))


#: A health state that is, by itself, a failing signal of the system's OWN
#: (never one inherited through a dependency cap — see `_own_failing`).
_STATE_FAILING_SIGNAL = {
    "suspended": "autonomy",
    "silent": "liveness",
    "stalled": "reliability",
    "failing": "reliability",
}
#: Below this a component is failing — the scorer's degraded threshold.
_FAILING_COMPONENT_BELOW = 0.8
_COMPONENTS = ("liveness", "behaviour", "reliability", "cost", "conformance", "oversight", "review")
#: The failing signal named when a system fails only because one of ITS
#: critical dependencies does (SRE: no better than a critical dependency).
FAILING_SIGNAL_DEPENDENCY = "dependency"


@dataclass
class Failing:
    """Why a system is failing: its own signal, or (transitively) a
    critical dependency's, with the dependency named."""

    signal: str
    via_system_id: Optional[str] = None


@dataclass
class SystemFacts:
    id: str
    name: str
    risk_tier: Optional[str]
    intended_client_systems: List[str]
    autonomy_level: Optional[str] = None
    system_kind: Optional[str] = None
    #: The core refuses callers outside
    #: `intended_client_systems` (else the finding is advisory).
    enforce_accepted_callers: bool = False


class CompositionIndex:
    """Everything composition needs about a tenant, loaded once.

    Queries: AI Systems, edges, cards, segments in the window, own-run
    counts in the window. Everything else is computed in memory, so the
    list (every system) and the detail view (one system) share the result.
    Observations (segments, A2A calls) are aggregated in SQL; the index
    holds per-pair aggregates and the latest few outcomes, never every row.

    ``scope_root`` — a DEPENDENCY-HEALTH index for one system (the health
    scorer runs once per system per tick; a tenant-wide index each time made
    a tick O(systems × tenant)). The observations are loaded only for the
    systems that system's dependency health can depend on: itself, its
    direct dependencies, and everything reachable from them along CRITICAL
    shared-service edges (the closure the failing propagation walks). Inside
    that closure every answer is exactly the tenant-wide one (parity:
    tests/test_composition_scoped_index.py); the tenant-wide questions
    (callers, role, findings) raise on a scoped index.
    """

    def __init__(
        self,
        db: Session,
        tenant_id: str,
        *,
        window_hours: int = DEFAULT_COMPOSITION_WINDOW_HOURS,
        now: Optional[datetime] = None,
        scope_root: Optional[str] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.window_hours = window_hours
        self.now = now or datetime.now(timezone.utc)
        window_start = self.now - timedelta(hours=window_hours)

        self.systems: Dict[str, SystemFacts] = {}
        for g in db.query(ResourceGroup).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        ):
            self.systems[g.id] = SystemFacts(
                id=g.id,
                name=g.display_name or g.name,
                risk_tier=g.eu_ai_act_risk_tier,
                intended_client_systems=list(g.intended_client_systems or []),
                autonomy_level=g.autonomy_level,
                system_kind=g.system_kind,
                enforce_accepted_callers=bool(g.enforce_accepted_callers),
            )

        self.edges: List[AISystemDependency] = (
            db.query(AISystemDependency)
            .filter(AISystemDependency.tenant_id == tenant_id)
            .order_by(AISystemDependency.ai_system_id, AISystemDependency.declared_at)
            .all()
        )
        self.edges_by_system: Dict[str, List[AISystemDependency]] = {}
        for e in self.edges:
            self.edges_by_system.setdefault(e.ai_system_id, []).append(e)

        # Card → system, from any declared shared_service edge in the tenant.
        self.card_system: Dict[str, str] = {}
        for e in self.edges:
            if e.role == ROLE_SHARED_SERVICE and e.agent_card_id and e.depends_on_system_id:
                self.card_system.setdefault(e.agent_card_id, e.depends_on_system_id)

        self.card_names: Dict[str, str] = {
            cid: name
            for cid, name in db.query(AgentCard.id, AgentCard.name).filter(
                AgentCard.tenant_id == tenant_id
            )
        }

        #: None = tenant-wide; else the systems whose observations are loaded.
        self.scope: Optional[Set[str]] = (
            self._dependency_scope(scope_root) if scope_root is not None else None
        )
        scoped = list(self.scope) if self.scope is not None else None

        # (caller, segment) → stats. The CALLER is the IMMEDIATE calling AI
        # System (`caller_system_id`): in A → B → C, C's
        # caller is B — the system that declares the edge, and the one
        # accepted-caller enforcement checks. `root_system_id` only says
        # which run a segment belongs to.
        #
        # Aggregated in SQL (performance, tests/test_composition_performance_pg.py):
        # the outcome mix per pair, then only the latest
        # DOWN_AFTER_CONSECUTIVE_FAILURES outcomes per pair — never every
        # segment of the window pulled into Python.
        self.pairs: Dict[Tuple[Optional[str], str], PairStats] = {}
        # The pair's FACTS (calls, outcome mix, last seen) count
        # every call; its RATES (judged counts, THE down rule's latest-N)
        # only calls outside every incident window of the service, of the
        # run's root, or of the tenant.
        seg_in_window: Tuple[Any, ...] = (
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.first_action_at >= window_start,
        )
        if scoped is not None:
            seg_in_window += (AISystemRunSegment.caller_system_id.in_(scoped),)
        judged_call = judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE)
        from sqlalchemy import case as _case

        # The call token: a refused delegation (`failed` +
        # `blocked_by_policy`) reads `blocked`.
        call = segment_call_outcome_sql(AISystemRunSegment)
        now = datetime.now(timezone.utc)
        held_judged = and_(judged_call, held_segment_expr(AISystemRunSegment))
        lapsed_judged = and_(judged_call, lapsed_held_segment_expr(AISystemRunSegment, now))
        for caller, seg, outcome, outcome_id, n, n_judged, n_held, n_lapsed, last_at in (
            db.query(
                AISystemRunSegment.caller_system_id,
                AISystemRunSegment.segment_system_id,
                call,
                AISystemRunSegment.outcome_id,
                func.count(AISystemRunSegment.id),
                func.sum(_case((judged_call, 1), else_=0)),
                func.sum(_case((held_judged, 1), else_=0)),
                func.sum(_case((lapsed_judged, 1), else_=0)),
                func.max(AISystemRunSegment.last_action_at),
            )
            .filter(*seg_in_window)
            .group_by(
                AISystemRunSegment.caller_system_id,
                AISystemRunSegment.segment_system_id,
                call,
                AISystemRunSegment.outcome_id,
            )
            .all()
        ):
            st = self.pairs.setdefault((caller, seg), PairStats())
            st.calls += int(n)
            st.by_outcome[outcome] = st.by_outcome.get(outcome, 0) + int(n)
            key = outcome_id or ""
            st.outcome_ids[key] = st.outcome_ids.get(key, 0) + int(n)
            st.judged_calls += int(n_judged or 0)
            st.judged_held += int(n_held or 0)
            st.judged_held_lapsed += int(n_lapsed or 0)
            if n_judged:
                st.judged_by_outcome[outcome] = st.judged_by_outcome.get(outcome, 0) + int(n_judged)
            last = _aware(last_at)
            if last is not None and (st.last_seen_at is None or last > st.last_seen_at):
                st.last_seen_at = last
        rn = func.row_number().over(
            partition_by=(AISystemRunSegment.caller_system_id, AISystemRunSegment.segment_system_id),
            order_by=(AISystemRunSegment.last_action_at.desc(), AISystemRunSegment.id.desc()),
        )
        latest = (
            db.query(
                AISystemRunSegment.caller_system_id.label("caller"),
                AISystemRunSegment.segment_system_id.label("seg"),
                segment_call_outcome_sql(AISystemRunSegment).label("outcome"),
                AISystemRunSegment.last_action_at.label("at"),
                _case((held_segment_expr(AISystemRunSegment), True), else_=False).label("held"),
                rn.label("rn"),
            )
            .filter(*seg_in_window, judged_call)
            .subquery()
        )
        for caller, seg, outcome, last_at, is_held in (
            db.query(latest.c.caller, latest.c.seg, latest.c.outcome, latest.c.at, latest.c.held)
            .filter(latest.c.rn <= DOWN_AFTER_CONSECUTIVE_FAILURES)
            .order_by(latest.c.caller, latest.c.seg, latest.c.rn)
            .all()
        ):
            st = self.pairs[(caller, seg)]
            if st.latest_outcome is None:
                st.latest_outcome = outcome
            st.recent.append((_aware(last_at), outcome))
            st.recent_held.append(bool(is_held))

        own_runs_q = db.query(AISystemRun.ai_system_id, func.count(AISystemRun.id)).filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id.isnot(None),
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= window_start,
        )
        if scoped is not None:
            own_runs_q = own_runs_q.filter(AISystemRun.ai_system_id.in_(scoped))
        self.own_runs: Dict[str, int] = {
            sid: int(n) for sid, n in own_runs_q.group_by(AISystemRun.ai_system_id).all()
        }

        # What the checks and dependency health read.
        from app.database.models.ai_system_health import AISystemHealth
        from app.services.ai_system_closure import a2a_call_targets, agent_grants_for_systems

        observed_systems = scoped if scoped is not None else list(self.systems)
        self.health_rows: Dict[str, AISystemHealth] = {
            h.ai_system_id: h
            for h in db.query(AISystemHealth).filter(
                AISystemHealth.tenant_id == tenant_id,
                AISystemHealth.ai_system_id.in_(observed_systems),
            )
        } if observed_systems else {}
        # Grants and contracts feed the composition checks only — a scoped
        # (dependency-health) index never answers those.
        grants = (
            agent_grants_for_systems(db, tenant_id, list(self.systems))
            if scoped is None else {}
        )
        #: system → agent cards its members' a2a_call grants reach
        self.granted_cards: Dict[str, Set[str]] = {
            sid: set(a2a_call_targets(g)) for sid, g in grants.items()
        }
        #: system → a member holds a wildcard (`*`) a2a_call grant
        self.wildcard_a2a: Set[str] = {
            sid for sid, gs in grants.items()
            if any(
                g.get("action_type") == "a2a_call" and g.get("effect") != "deny"
                and g.get("target") == "*"
                for g in gs
            )
        }
        #: system → (active contract id, version)
        self.active_contracts: Dict[str, Tuple[str, int]] = {
            sid: (cid, ver)
            for sid, cid, ver in db.query(
                AISystemContract.ai_system_id, AISystemContract.id, AISystemContract.version
            ).filter(
                AISystemContract.tenant_id == tenant_id,
                AISystemContract.status == CONTRACT_ACTIVE,
            )
        } if scoped is None else {}
        self.contract_versions: Dict[str, int] = {}
        pins = [e.contract_pin for e in self.edges if e.contract_pin] if scoped is None else []
        if pins:
            self.contract_versions = {
                cid: ver
                for cid, ver in db.query(AISystemContract.id, AISystemContract.version).filter(
                    AISystemContract.tenant_id == tenant_id,
                    AISystemContract.id.in_(pins),
                )
            }
        # Outbound A2A calls through the gateway in the window, by calling
        # system and card: `(system, card) → {calls, errors, last_seen_at}`.
        # The card is `proxy_logs.server_id` for a2a rows; the caller is the
        # nearest AI System of the row's resource group (the ledger's rule).
        self.a2a_calls: Dict[Tuple[str, str], Dict[str, Any]] = {}
        from sqlalchemy import case

        from app.constants.proxy import ProxySubtype
        from app.database.models.proxy_log import ProxyLog

        group_system = nearest_ai_system_map(db, tenant_id)
        a2a_groups = (
            [g for g, sid in group_system.items() if sid == scope_root]
            if scoped is not None else None
        )
        is_error = case(
            ((ProxyLog.http_status_code >= 400) | ProxyLog.error_type.isnot(None), 1), else_=0
        )
        # Calls the core REFUSED because the caller is not
        # an accepted client of an enforcing service. They never reach the
        # service, so they are in no segment: this is the only place the
        # attempt is visible.
        is_refused = case(
            (ProxyLog.proxy_subtype == ProxySubtype.UNACCEPTED_CALLER.value, 1), else_=0
        )
        # The call count and refusals are FACTS (an undeclared
        # external dependency seen only during an outage is still one); the
        # boundary's error share is a RATE, over calls whose RUN lies outside
        # every incident window (proxy-log rows are judged through their run).
        judged_row = judged_log(ProxyLog, tenant_id, purpose=PURPOSE_RATE)
        a2a_q = db.query(
            ProxyLog.resource_group_id,
            ProxyLog.server_id,
            func.count(ProxyLog.id),
            func.sum(is_error),
            func.sum(is_refused),
            func.max(ProxyLog.timestamp),
            func.sum(case((judged_row, 1), else_=0)),
            func.sum(case((judged_row, is_error), else_=0)),
        ).filter(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.proxy_type == "a2a",
            ProxyLog.timestamp >= window_start,
            ProxyLog.server_id.isnot(None),
            ProxyLog.resource_group_id.isnot(None),
        )
        if a2a_groups is not None:
            # A scoped index reads only the root's own outbound calls (the
            # external-dependency boundary of its dependency health).
            a2a_q = a2a_q.filter(ProxyLog.resource_group_id.in_(a2a_groups or [""]))
        for rg, card, n, errors, refused, last, n_judged, judged_errors in (
            a2a_q.group_by(ProxyLog.resource_group_id, ProxyLog.server_id).all()
        ):
            caller = group_system.get(rg)
            if caller is None or caller not in self.systems:
                continue
            agg = self.a2a_calls.setdefault(
                (caller, card),
                {"calls": 0, "errors": 0, "refused_unaccepted": 0, "last_seen_at": None,
                 "judged_calls": 0, "judged_errors": 0},
            )
            agg["calls"] += int(n or 0)
            agg["errors"] += int(errors or 0)
            agg["judged_calls"] += int(n_judged or 0)
            agg["judged_errors"] += int(judged_errors or 0)
            agg["refused_unaccepted"] += int(refused or 0)
            last = _aware(last)
            if last is not None and (agg["last_seen_at"] is None or last > agg["last_seen_at"]):
                agg["last_seen_at"] = last

        # Open drift of each system's OWN (any basis — a service's segment
        # drift included), read directly rather than through the health row,
        # so "a critical dependency is drifting" reaches its callers on the
        # next tick, not after the dependency's own health refresh. The
        # dependency findings raised ON a root are excluded (not its own).
        from app.database.models.ai_system_drift import AISystemDriftEvent
        from app.services.ai_system_service_signals import is_own_drift

        self.open_drift: Dict[str, int] = {}
        drift_q = db.query(AISystemDriftEvent.ai_system_id, AISystemDriftEvent.metric_key).filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
        if scoped is not None:
            drift_q = drift_q.filter(AISystemDriftEvent.ai_system_id.in_(scoped))
        for sid, key in drift_q:
            if is_own_drift(key):
                self.open_drift[sid] = self.open_drift.get(sid, 0) + 1

        self._build_lookups()
        self._effective = self._effective_tiers()
        self._cycles = self._cycle_members()
        self._failing = self._failing_systems()

    def _dependency_scope(self, root: str) -> Set[str]:
        """`root`, its direct dependencies, and everything reachable from
        them along CRITICAL shared-service edges — every system whose
        observations `dependency_status(root, …)` can read (the failing
        propagation in `_failing_systems` follows critical edges only)."""
        critical: Dict[str, List[str]] = {}
        for e in self.edges:
            if e.role == ROLE_SHARED_SERVICE and e.criticality == "critical" and e.depends_on_system_id:
                critical.setdefault(e.ai_system_id, []).append(e.depends_on_system_id)
        scope = {root}
        frontier = [e.depends_on_system_id for e in self.edges_by_system.get(root, [])
                    if e.depends_on_system_id]
        while frontier:
            sid = frontier.pop()
            if sid in scope:
                continue
            scope.add(sid)
            frontier.extend(critical.get(sid, []))
        return scope

    def _tenant_wide(self, what: str) -> None:
        if self.scope is not None:
            raise RuntimeError(
                f"CompositionIndex.{what} is a tenant-wide question; this index is "
                f"scoped to one system's dependency health"
            )

    def _build_lookups(self) -> None:
        """Reverse maps over the loaded facts, built once. Every per-system
        question the list asks for each of its rows is then a dict lookup,
        not a scan of the tenant's pairs, edges or A2A calls (which made the
        list O(systems × tenant))."""
        self._declared_tiers = {k: f.risk_tier for k, f in self.systems.items()}
        self._names = {k: f.name for k, f in self.systems.items()}
        self._targets_of: Dict[Optional[str], Dict[str, PairStats]] = {}
        self._callers_of: Dict[str, Dict[str, PairStats]] = {}
        for (caller, seg), st in self.pairs.items():
            self._targets_of.setdefault(caller, {})[seg] = st
            if caller is not None:
                self._callers_of.setdefault(seg, {})[caller] = st
        self._edge_callers: Dict[str, Set[str]] = {}
        #: target system → the edges declared ON it (declaration order)
        self._edges_to: Dict[str, List[AISystemDependency]] = {}
        for e in self.edges:
            if e.depends_on_system_id:
                self._edges_to.setdefault(e.depends_on_system_id, []).append(e)
            if e.role == ROLE_SHARED_SERVICE and e.depends_on_system_id:
                self._edge_callers.setdefault(e.depends_on_system_id, set()).add(e.ai_system_id)
        self._cards_of_system: Dict[str, Set[str]] = {}
        for card, sid in self.card_system.items():
            self._cards_of_system.setdefault(sid, set()).add(card)
        self._a2a_from: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        self._refused: Dict[str, Dict[str, int]] = {}
        for (caller, card), agg in sorted(self.a2a_calls.items()):
            self._a2a_from.setdefault(caller, []).append((card, agg))
            target = self.card_system.get(card)
            if agg.get("refused_unaccepted") and target is not None:
                per = self._refused.setdefault(target, {})
                per[caller] = per.get(caller, 0) + int(agg["refused_unaccepted"])

    # -- names ---------------------------------------------------------------

    def system_name(self, sid: Optional[str]) -> Optional[str]:
        if sid is None:
            return None
        facts = self.systems.get(sid)
        return facts.name if facts else None

    def card_name(self, cid: Optional[str]) -> Optional[str]:
        return self.card_names.get(cid) if cid else None

    # -- observed ------------------------------------------------------------

    def pair(self, caller: str, segment: str) -> PairStats:
        return self.pairs.get((caller, segment), _EMPTY)

    def observed_targets(self, sid: str) -> Dict[str, PairStats]:
        """Systems this one called directly, in the window."""
        if self.scope is not None and sid not in self.scope:
            self._tenant_wide("observed_targets")
        return dict(self._targets_of.get(sid, {}))

    def observed_callers(self, sid: str) -> Dict[str, PairStats]:
        """Systems that called this one directly, in the window (a caller in
        no AI System is not a caller anyone can declare, so it is left out)."""
        self._tenant_wide("observed_callers")
        return dict(self._callers_of.get(sid, {}))

    def a2a_calls_from(self, sid: str) -> List[Tuple[str, Dict[str, Any]]]:
        """`[(card, {calls, errors, refused_unaccepted, last_seen_at})]` of the
        A2A calls this system made through the gateway, sorted by card."""
        self._tenant_wide("a2a_calls_from")
        return list(self._a2a_from.get(sid, []))

    def cards_of_system(self, sid: str) -> Set[str]:
        """The agent cards a declared shared-service edge resolved to `sid`."""
        return set(self._cards_of_system.get(sid, set()))

    # -- declared ------------------------------------------------------------

    def declared_callers(self, sid: str) -> Set[str]:
        """Systems that declare an edge to this one."""
        return set(self._edge_callers.get(sid, set()))

    def accepted_callers(self, sid: str) -> Set[str]:
        facts = self.systems.get(sid)
        return set(facts.intended_client_systems) if facts else set()

    def enforces_accepted_callers(self, sid: str) -> bool:
        facts = self.systems.get(sid)
        return bool(facts and facts.enforce_accepted_callers)

    def refused_callers(self, sid: str) -> Dict[str, int]:
        """`{caller system: attempts}` the core refused as unaccepted callers
        of `sid` in the window (the card resolved to `sid` through a declared
        shared-service edge — the same rule the core uses to enforce)."""
        self._tenant_wide("refused_callers")
        return dict(self._refused.get(sid, {}))

    # -- role ------------------------------------------------------------------

    def role(self, sid: str) -> str:
        self._tenant_wide("role")
        is_service = bool(
            self.declared_callers(sid) or self.accepted_callers(sid) or self.observed_callers(sid)
        )
        if not is_service:
            return ROLE_ROOT
        is_root = self.own_runs.get(sid, 0) > 0 or bool(self.edges_by_system.get(sid))
        return ROLE_ROOT_AND_SERVICE if is_root else ROLE_SERVICE

    def has_external_dependencies(self, sid: str) -> bool:
        return any(e.role == ROLE_EXTERNAL for e in self.edges_by_system.get(sid, []))

    def matches_role_filter(self, sid: str, role_filter: str) -> bool:
        if role_filter == ROLE_FILTER_EXTERNAL_DEPS:
            return self.has_external_dependencies(sid)
        role = self.role(sid)
        if role_filter == ROLE_FILTER_ROOT:
            return role in (ROLE_ROOT, ROLE_ROOT_AND_SERVICE)
        if role_filter == ROLE_FILTER_SHARED_SERVICE:
            return role in (ROLE_SERVICE, ROLE_ROOT_AND_SERVICE)
        return True

    # -- risk ----------------------------------------------------------------

    def _effective_tiers(self) -> Dict[str, Optional[str]]:
        return propagate_effective_tiers(self._declared_tiers, self.edges)

    def risk_tier_effective(self, sid: str) -> Optional[str]:
        return self._effective.get(sid)

    def effective_tier_sources(self, sid: str) -> List[Dict[str, Any]]:
        """The callers that raised this system's tier above its declared one."""
        return effective_tier_sources_of(
            sid,
            self._declared_tiers,
            self._effective,
            # Only edges declared ON `sid` can raise its tier.
            self._edges_to.get(sid, []),
            self._names,
        )

    def separability_edges(self, sid: str) -> List[AISystemDependency]:
        """Every edge carrying a separability claim that touches this system
        — one it declares, or one declared on it."""
        touching = self.edges_by_system.get(sid, []) + [
            e for e in self._edges_to.get(sid, []) if e.ai_system_id != sid
        ]
        return [
            e for e in touching
            if not e.contributes_to_decision and bool(e.separability_rationale)
        ]

    def separability_declared(self, sid: str) -> bool:
        """A separability claim touches this system: an edge it declares, or
        an edge declared on it, says the dependency does NOT contribute to
        the decision (with its rationale — the API refuses one without).
        Shown on both systems."""
        return bool(self.separability_edges(sid))

    def separability_sealed(self, sid: str) -> bool:
        """Every separability claim touching this system is sealed (a
        declaration record). False when there is none — nothing is sealed."""
        claims = self.separability_edges(sid)
        return bool(claims) and all(e.separability_record_id for e in claims)

    def separability_declarations(self, sid: str) -> List[Dict[str, Any]]:
        """The sealed separability declarations, as both systems show
        them: the claim, its rationale, and the sealed record."""
        return [
            {
                "edge_id": e.id,
                "ai_system_id": e.ai_system_id,
                "ai_system_name": self.system_name(e.ai_system_id),
                "depends_on_system_id": e.depends_on_system_id,
                "depends_on_system_name": self.system_name(e.depends_on_system_id),
                "agent_card_id": e.agent_card_id,
                "agent_card_name": self.card_name(e.agent_card_id),
                "side": "declares" if e.ai_system_id == sid else "declared_on",
                "separability_rationale": e.separability_rationale,
                "record_id": e.separability_record_id,
                "sealed_at": _iso(e.separability_sealed_at),
                "sealed": bool(e.separability_record_id),
            }
            for e in sorted(self.separability_edges(sid), key=lambda e: e.id)
        ]

    # -- cycles ----------------------------------------------------------------

    def _cycle_members(self) -> Dict[str, str]:
        """`{system on a cycle: its successor on that cycle}` over declared
        shared_service edges (Tarjan's SCCs; a node in a non-trivial SCC is
        on a cycle)."""
        graph: Dict[str, List[str]] = {}
        for e in self.edges:
            if e.role == ROLE_SHARED_SERVICE and e.depends_on_system_id:
                graph.setdefault(e.ai_system_id, []).append(e.depends_on_system_id)
        index: Dict[str, int] = {}
        low: Dict[str, int] = {}
        stack: List[str] = []
        on_stack: Set[str] = set()
        sccs: List[List[str]] = []
        counter = [0]

        def strongconnect(v: str) -> None:
            # Iterative Tarjan: an estate graph is small, but recursion depth
            # must not depend on it.
            work = [(v, iter(graph.get(v, [])))]
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            on_stack.add(v)
            while work:
                node, children = work[-1]
                advanced = False
                for w in children:
                    if w not in index:
                        index[w] = low[w] = counter[0]
                        counter[0] += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(graph.get(w, []))))
                        advanced = True
                        break
                    if w in on_stack:
                        low[node] = min(low[node], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    sccs.append(comp)

        for v in sorted(graph):
            if v not in index:
                strongconnect(v)

        members: Dict[str, str] = {}
        for comp in sccs:
            if len(comp) < 2:
                continue  # self-edges are rejected by the schema
            comp_set = set(comp)
            for v in comp:
                succ = sorted(w for w in graph.get(v, []) if w in comp_set)
                members[v] = succ[0]
        return members

    # -- dependency health -------------------------------------

    def _own_failing(self, sid: str) -> Optional[str]:
        """The failing signal of a system's OWN, from its materialised health.

        Deliberately not `health_state == 'degraded'`: a root capped at
        degraded BY a dependency is degraded too, and reading that back would
        let two systems on a dependency cycle hold each other down forever
        after either recovered. Only the system's own evidence counts here;
        the dependency part is propagated explicitly in `_failing_systems`.
        """
        facts = self.systems.get(sid)
        if facts is not None and facts.autonomy_level == "suspended":
            return "autonomy"
        if self.open_drift.get(sid, 0) > 0:
            return "behaviour"
        row = self.health_rows.get(sid)
        if row is None:
            return None
        signal = _STATE_FAILING_SIGNAL.get(row.health_state)
        if signal:
            return signal
        # Only the signals its kind ENFORCES: an advisory score is shown on
        # its panel but is no evidence of ill health (signal profile).
        from app.services.ai_system_signal_profile import ENFORCED, profile_for

        levels = profile_for(facts.system_kind if facts is not None else None)
        scored = [
            (getattr(row, f"{c}_score", None), c)
            for c in _COMPONENTS
            if getattr(row, f"{c}_score", None) is not None
            and levels.get(c, ENFORCED) == ENFORCED
        ]
        if scored:
            worst, name = min(scored, key=lambda sv: (sv[0], _COMPONENTS.index(sv[1])))
            if worst < _FAILING_COMPONENT_BELOW:
                return name
        return None

    def _failing_systems(self) -> Dict[str, Failing]:
        """Every failing system, own evidence first, then propagated along
        CRITICAL shared-service edges to a fixed point: a system is no
        healthier than a critical dependency (SRE), transitively. Monotone
        over a finite set, so it terminates on cycles — and because the base
        is own evidence only, a cycle cannot sustain a failure nobody has."""
        failing: Dict[str, Failing] = {}
        judged = self.scope if self.scope is not None else self.systems
        for sid in judged:
            own = self._own_failing(sid)
            if own:
                failing[sid] = Failing(own)
        # A scoped index judges its closure only (closed under critical
        # edges, so nothing inside it reads a system outside it).
        edges = [e for e in self.edges if e.ai_system_id in judged]
        changed = True
        while changed:
            changed = False
            for e in edges:
                if (
                    e.role != ROLE_SHARED_SERVICE
                    or e.criticality != "critical"
                    or not e.depends_on_system_id
                    or e.ai_system_id in failing
                ):
                    continue
                target = e.depends_on_system_id
                unanswered = self.pair(e.ai_system_id, target).calls_verdict() not in (
                    None, CALLS_OK
                )
                if unanswered or target in failing:
                    failing[e.ai_system_id] = Failing(FAILING_SIGNAL_DEPENDENCY, target)
                    changed = True
        return failing

    def failing(self, sid: str) -> Optional[Failing]:
        return self._failing.get(sid)

    def dependency_status(
        self, sid: str, edge: Optional[AISystemDependency]
    ) -> Tuple[str, Optional[str]]:
        """`(health, failing_signal)` of one dependency, as `sid` sees it.

        THE dependency health: the Composition tab's node, the AI Systems
        list's worst dependency and the root's verdict input all read it.
        Responsiveness to THIS caller first, by THE down rule (the latest
        calls all unanswered is a `liveness` failure — an outage — whatever
        the service's own tile says; some unanswered is `reliability`); then the
        service's own failing signal, or a critical dependency of its own
        that fails (`dependency`). External edges are assumed, never tested.
        """
        health = edge_health(
            edge,
            self.pair(sid, edge.depends_on_system_id)
            if edge is not None and edge.depends_on_system_id
            else None,
        )
        if health == HEALTH_DEGRADED:
            verdict = self.pair(sid, edge.depends_on_system_id).calls_verdict()
            return health, (
                FAILING_SIGNAL_NOT_ANSWERING if verdict == CALLS_DOWN
                else FAILING_SIGNAL_SOME_UNANSWERED
            )
        if health in (HEALTH_HEALTHY, HEALTH_IDLE) and edge is not None:
            failing = self._failing.get(edge.depends_on_system_id or "")
            if failing is not None:
                return HEALTH_DEGRADED, failing.signal
        return health, None

    # -- list summary ----------------------------------------------------------

    def summary(self, sid: str) -> Dict[str, Any]:
        """What the AI Systems list carries per system (besides member counts)."""
        self._tenant_wide("summary")
        edges = self.edges_by_system.get(sid, [])
        healths = [self.dependency_status(sid, e)[0] for e in edges]
        observed_callers = self.observed_callers(sid)
        declared = self.declared_callers(sid) | self.accepted_callers(sid)
        role = self.role(sid)
        return {
            "depends_on": len(edges),
            "worst_dependency_health": worst_health(healths),
            "used_by_declared": len(declared),
            "used_by_observed": len(observed_callers),
            # A service with no callers in the window reads `idle`, never an
            # empty cell.
            "idle": role != ROLE_ROOT and not observed_callers,
            "role": role,
            "has_external_dependencies": self.has_external_dependencies(sid),
            "risk_tier_effective": self.risk_tier_effective(sid),
            "effective_tier_sources": self.effective_tier_sources(sid),
            "separability_declared": self.separability_declared(sid),
            "separability_sealed": self.separability_sealed(sid),
            "enforce_accepted_callers": self.enforces_accepted_callers(sid),
            "composition_finding_count": self.composition_finding_count(sid),
        }

    def composition_finding_count(self, sid: str) -> int:
        """How many composition findings the system has — the list's count
        and the Composition tab's list are one computation."""
        self._tenant_wide("composition_finding_count")
        return len(_findings(self, sid))


def _findings(index: "CompositionIndex", sid: str) -> List[Dict[str, Any]]:
    """Composition findings live in the assurance check library — one fact,
    one computation: the inbox, the AI Systems list count and the
    Composition tab all read `composition_findings`. Imported here, not at
    module top, because the check library imports this module."""
    from app.services.assurance_checks.composition import composition_findings

    return composition_findings(index, sid)


# ---------------------------------------------------------------------------
# Composition view (GET /v1/admin/ai-systems/{id}/composition)
# ---------------------------------------------------------------------------


def _group_names(db: Session, tenant_id: str, ids: Iterable[str]) -> Dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        gid: (dn or n)
        for gid, n, dn in db.query(
            ResourceGroup.id, ResourceGroup.name, ResourceGroup.display_name
        ).filter(ResourceGroup.tenant_id == tenant_id, ResourceGroup.id.in_(ids))
    }


def _ref(gid: Optional[str], names: Dict[str, str]) -> Optional[Dict[str, Any]]:
    return {"id": gid, "name": names.get(gid)} if gid else None


def _flatten(blob: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    if not isinstance(blob, dict):
        return []
    out: List[Tuple[str, Any]] = []
    for key in sorted(blob):
        value = blob[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_flatten(value, path))
        elif value is not None:
            out.append((path, value))
    return out


def _dig(blob: Any, path: str) -> Any:
    cur = blob
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _limit_kind(path: str) -> str:
    p = path.lower()
    if "budget" in p or "usd" in p or "cost" in p or "spend" in p:
        return "budget"
    if "concurren" in p:
        return "concurrency"
    if "token" in p or p.split(".")[-1] in ("tpm", "itpm", "otpm"):
        return "tokens"
    return "rate"


def _gateway(closure: Dict[str, Any], chain: List[ResourceGroup], names: Dict[str, str],
             system_id: str) -> Dict[str, Any]:
    from app.database.models.ai_system_response_policy import describe_action
    from app.services.ai_system_closure import _GOVERNANCE_FIELDS, _LIMIT_FIELDS
    from app.services.ai_system_envelope import ENFORCED_TERMS

    comp = closure.get("composition") or {}
    policies: List[Dict[str, Any]] = []
    for section, kind in (
        ("guardrails", "guardrail"),
        ("argument_policies", "argument_policy"),
        ("cache_configs", "cache"),
    ):
        for item in comp.get(section) or []:
            policies.append({
                "kind": kind,
                "id": item.get("id"),
                "name": item.get("name") or item.get("id"),
                "summary": "enabled" if item.get("enabled", True) else "disabled",
                "inherited_from": _ref(item.get("bound_via"), names) if item.get("inherited") else None,
            })
    for item in closure.get("response_policies") or []:
        actions = ", ".join(describe_action(a) for a in (item.get("actions") or []))
        policies.append({
            "kind": "response_policy",
            "id": item.get("id"),
            "name": item.get("name") or item.get("id"),
            "summary": (
                f"on {item.get('signal_kind')}"
                + (f" → {actions}" if actions else "")
                + ("" if item.get("enabled") else " (disabled)")
            ),
            "inherited_from": _ref(item.get("attached_to"), names) if item.get("inherited") else None,
        })

    def nearest_setter(field_name: str, path: Optional[str], value: Any) -> Optional[str]:
        """The nearest group in the chain whose own blob sets `path` to the
        effective `value` (min-wins: the one that bound it)."""
        for g in chain:
            blob = getattr(g, field_name, None)
            if path is None:
                if isinstance(blob, dict) and blob:
                    return g.id
            elif _dig(blob, path) == value:
                return g.id
        return None

    governance = closure.get("governance") or {}
    for domain, field_name in _GOVERNANCE_FIELDS:
        blob = governance.get(domain) or {}
        leaves = _flatten(blob)
        if not leaves:
            continue
        setter = nearest_setter(field_name, None, None)
        keys = ", ".join(p for p, _ in leaves[:4]) + ("…" if len(leaves) > 4 else "")
        policies.append({
            "kind": "governance",
            "id": None,
            "name": f"{domain.upper()} governance",
            "summary": keys,
            "inherited_from": _ref(setter, names) if setter and setter != system_id else None,
        })

    limits: List[Dict[str, Any]] = []
    for domain, field_name in _LIMIT_FIELDS:
        for path, value in _flatten((closure.get("limits") or {}).get(domain)):
            setter = nearest_setter(field_name, path, value)
            limits.append({
                "kind": _limit_kind(path),
                "key": f"{domain}.{path}",
                "value": value,
                "summary": f"{domain.upper()} {path.replace('.', ' ')}: {value}",
                "inherited_from": _ref(setter, names) if setter and setter != system_id else None,
            })
    for term, value in sorted((closure.get("operating_envelope") or {}).items()):
        limits.append({
            "kind": "run_cap" if term in ENFORCED_TERMS else "envelope",
            "key": term,
            "value": value,
            "summary": f"{term.replace('_', ' ')}: {value}",
            "inherited_from": None,
        })
    return {"policies": policies, "limits": limits}


def _resource_list(items: Optional[List[Dict[str, Any]]], names: Dict[str, str]) -> List[Dict[str, Any]]:
    return [
        {
            "id": i.get("id"),
            "name": i.get("name") or i.get("id"),
            "inherited_from": _ref(i.get("bound_via"), names) if i.get("inherited") else None,
        }
        for i in (items or [])
    ]


def _mcp_resource_list(
    db: Session, tenant_id: str, items: Optional[List[Dict[str, Any]]], names: Dict[str, str]
) -> List[Dict[str, Any]]:
    """`_resource_list` plus `system`: the binding is one of the platform's
    own system MCP servers (Agent Skills, …), recognised by the same rule
    the proxy routes on (`is_system_server`)."""
    from app.constants.system_server import is_system_server
    from app.database.models.mcp_server import MCPServer
    from app.database.models.server_config import ServerConfig

    out = _resource_list(items, names)
    ids = [r["id"] for r in out if r["id"]]
    system_ids: Set[str] = set()
    server_of: Dict[str, str] = {}
    if ids:
        for cfg_id, server_id, base_url in (
            db.query(ServerConfig.id, MCPServer.id, MCPServer.base_url)
            .join(MCPServer, MCPServer.id == ServerConfig.mcp_server_id)
            .filter(ServerConfig.tenant_id == tenant_id, ServerConfig.id.in_(ids))
        ):
            server_of[cfg_id] = server_id
            if base_url and is_system_server(base_url):
                system_ids.add(cfg_id)
    for r in out:
        r["system"] = r["id"] in system_ids
        # The run overlay places a run's MCP actions (server_id = the
        # mcp_servers id) on this binding.
        r["mcp_server_id"] = server_of.get(r["id"])
    return out


def _contract_ref(db: Session, tenant_id: str, system_id: str,
                  pin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from app.services.ai_system_closure import dependency_contract

    ref = dependency_contract(db, tenant_id, system_id, pin)
    return {"id": ref["id"], "version": ref["version"]} if ref else None


def dependency_edge_dict(edge: AISystemDependency, index: CompositionIndex) -> Dict[str, Any]:
    """The DependencyEdge wire shape: the row plus resolved names."""
    d = edge.to_dict()
    d["depends_on_system_name"] = index.system_name(edge.depends_on_system_id)
    d["agent_card_name"] = index.card_name(edge.agent_card_id)
    return d


def build_composition(
    db: Session,
    system: ResourceGroup,
    *,
    window_hours: int = DEFAULT_COMPOSITION_WINDOW_HOURS,
    index: Optional[CompositionIndex] = None,
) -> Dict[str, Any]:
    """The Composition tab payload — see the contract in the endpoint."""
    from app.services.ai_system_closure import inheritance_chain, resolve_closure
    from app.services.ai_system_human_approval import declared_human_approval

    tenant_id = system.tenant_id
    index = index or CompositionIndex(db, tenant_id, window_hours=window_hours)
    closure = resolve_closure(db, system)
    comp = closure.get("composition") or {}
    chain = inheritance_chain(system)

    ancestors = system.get_ancestors()
    names = _group_names(
        db, tenant_id,
        [g.id for g in ancestors]
        + [g.id for g in chain]
        + [str(p.get("attached_to")) for p in closure.get("response_policies") or []],
    )
    names[system.id] = system.display_name or system.name

    active = _contract_ref(db, tenant_id, system.id)

    # Delegation nodes: every card an a2a_call grant reaches (not denied),
    # joined with its declared edge; plus every declared edge.
    from app.services.ai_system_closure import a2a_call_targets

    granted_cards: Set[str] = set(a2a_call_targets(closure.get("agent_grants") or []))
    edges = index.edges_by_system.get(system.id, [])
    edge_by_card = {e.agent_card_id: e for e in edges if e.agent_card_id}

    dependencies: List[Dict[str, Any]] = []
    #: edge id → (health, failing signal): THE dependency status,
    #: computed once and read by both the node's health and its connection.
    dependency_statuses: Dict[str, Tuple[str, Optional[str]]] = {}
    for e in edges:
        stats = index.pair(system.id, e.depends_on_system_id) if e.depends_on_system_id else None
        node = dependency_edge_dict(e, index)
        dependency_statuses[e.id] = index.dependency_status(system.id, e)
        node.update({
            "declared": True,
            "granted": bool(e.agent_card_id and e.agent_card_id in granted_cards),
            "health": dependency_statuses[e.id][0],
            "observed": (stats or _EMPTY).observed(),
            "target_contract": (
                _target_contract(db, tenant_id, e) if e.role == ROLE_SHARED_SERVICE else None
            ),
        })
        dependencies.append(node)
    for card_id in sorted(granted_cards - set(edge_by_card)):
        resolved = index.card_system.get(card_id)
        stats = index.pair(system.id, resolved) if resolved else None
        dependencies.append({
            "id": None,
            "ai_system_id": system.id,
            "role": None,
            "depends_on_system_id": resolved,
            "depends_on_system_name": index.system_name(resolved),
            "agent_card_id": card_id,
            "agent_card_name": index.card_name(card_id),
            "criticality": None,
            "fallback": None,
            "contributes_to_decision": None,
            "separability_rationale": None,
            "separability_record_id": None,
            "separability_sealed_at": None,
            "supplier_legal_entity": None,
            "contract_pin": None,
            "assumptions": {},
            "declared_by": None,
            "declared_at": None,
            "updated_by": None,
            "updated_at": None,
            "declared": False,
            "granted": True,
            "health": HEALTH_UNKNOWN,
            "observed": (stats or _EMPTY).observed(),
            "target_contract": None,
        })

    declared_targets = {
        e.depends_on_system_id for e in edges
        if e.role == ROLE_SHARED_SERVICE and e.depends_on_system_id
    }
    observed_undeclared = [
        {
            "system_id": target,
            "system_name": index.system_name(target),
            "calls": st.calls,
            "last_seen_at": _iso(st.last_seen_at),
        }
        for target, st in sorted(index.observed_targets(system.id).items())
        if target not in declared_targets
    ]

    accepted = index.accepted_callers(system.id)
    edge_callers = index.declared_callers(system.id)
    observed_callers = index.observed_callers(system.id)
    dependents = [
        {
            "system_id": caller,
            "system_name": index.system_name(caller),
            "declared": caller in accepted,
            "edge_declared": caller in edge_callers,
            "observed_calls": observed_callers[caller].calls if caller in observed_callers else 0,
            "last_seen_at": (
                _iso(observed_callers[caller].last_seen_at) if caller in observed_callers else None
            ),
        }
        for caller in sorted(accepted | edge_callers | set(observed_callers))
    ]

    from app.services.ai_system_conformance import observed_clients

    label_calls = observed_clients(db, tenant_id, system.id, window_hours)
    declared_labels = [lbl for lbl in (system.intended_client_labels or []) if lbl]
    client_labels = [
        {"label": lbl, "declared": lbl in declared_labels, "observed_calls": label_calls.get(lbl, 0)}
        for lbl in sorted(set(declared_labels) | set(label_calls))
    ]

    resources = {
        "models": _resource_list(comp.get("llm_models"), names),
        "mcp_servers": _mcp_resource_list(db, tenant_id, comp.get("mcp_servers"), names),
        "skills": _resource_list(comp.get("skills"), names),
        "knowledge_bases": _resource_list(comp.get("knowledge_bases"), names),
    }
    # The Simple graph's connector lines: one status per connection, one rule.
    system_status = attach_connections(
        db, index, system, resources, dependencies, observed_undeclared, dependency_statuses
    )

    return {
        "system": {
            "id": system.id,
            "name": system.display_name or system.name,
            "system_kind": system.system_kind,
            "owner": system.owner,
            "lifecycle_stage": system.lifecycle_stage,
            "autonomy_level": closure.get("autonomy_level"),
            "active_contract": active,
            "risk_tier_declared": system.eu_ai_act_risk_tier,
            "risk_tier_effective": index.risk_tier_effective(system.id),
            "effective_tier_sources": index.effective_tier_sources(system.id),
            "separability_declared": index.separability_declared(system.id),
            "separability_declarations": index.separability_declarations(system.id),
            "enforce_accepted_callers": bool(system.enforce_accepted_callers),
        },
        "window_hours": window_hours,
        "location": [
            {
                "id": g.id,
                "name": g.display_name or g.name,
                "group_type": getattr(g.group_type, "value", g.group_type),
            }
            for g in [*ancestors, system]
        ],
        "agents": member_agents(db, tenant_id, comp.get("agent_identities") or []),
        "resources": resources,
        "system_status": system_status,
        "gateway": _gateway(closure, chain, names, system.id),
        "human_approval": declared_human_approval(db, tenant_id, closure, resources, dependencies),
        "dependencies": dependencies,
        "observed_undeclared": observed_undeclared,
        "dependents": dependents,
        "client_labels": client_labels,
        # The SAME computation the check library runs into the inbox.
        "findings": _findings(index, system.id),
    }


def member_agents(
    db: Session, tenant_id: str, closure_agents: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The system's member agents as the Composition tab and the assurance
    report show them: each with its declaration (read from
    the CLOSURE entry — the same terms the contract signs) and its CURRENT
    release (the one seen last) with trust and derived approval, or
    `release: null` when the identity has never declared one."""
    from app.services.agent_implementation import compact_release, current_releases

    current = current_releases(db, tenant_id, [a.get("id") for a in closure_agents])
    out = []
    for a in closure_agents:
        approved = a.get("approved_versions") or []
        out.append({
            "id": a.get("id"),
            "name": a.get("name") or a.get("id"),
            "implementation_name": a.get("implementation_name"),
            "approved_versions": list(approved),
            "require_release": bool(a.get("require_release")),
            "release": compact_release(current.get(a.get("id")), approved),
        })
    return out


def _target_contract(db: Session, tenant_id: str, edge: AISystemDependency) -> Optional[Dict[str, Any]]:
    ref = _contract_ref(db, tenant_id, edge.depends_on_system_id, pin=edge.contract_pin)
    if ref is None:
        return None
    ref["pinned"] = edge.contract_pin is not None
    return ref


__all__ = [
    "ASSUMPTION_EVIDENCE_KINDS",
    "COMPOSITION_FINDING_CODES",
    "DependencyAssumptions",
    "TierFacts",
    "effective_tier_sources_of",
    "load_tier_facts",
    "max_tier",
    "propagate_effective_tiers",
    "validate_assumptions",
    "FAILING_SIGNAL_DEPENDENCY",
    "Failing",
    "CompositionIndex",
    "DEFAULT_COMPOSITION_WINDOW_HOURS",
    "DEPENDENCY_HEALTHS",
    "DependencyConflictError",
    "DependencyValidationError",
    "RISK_TIER_ORDER",
    "ROLE_FILTERS",
    "SYSTEM_ROLES",
    "build_composition",
    "dependency_edge_dict",
    "member_agents",
    "edge_health",
    "load_ai_system",
    "nearest_ai_system_map",
    "tier_rank",
    "validate_dependency",
    "validate_intended_client_systems",
    "worst_health",
]
