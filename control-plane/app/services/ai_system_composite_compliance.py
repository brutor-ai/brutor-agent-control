# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems — risk and compliance

The facts about a system's dependency edges that compliance reads — one
computation for every surface that shows them:

  * **supplier information** (`supplier_information` evidence, EU AI Act
    Art 25(4)): an edge supplied by someone other than the deployer — every
    `external` dependency, and a `shared_service` whose
    `supplier_legal_entity` is set — needs the written agreement the root
    relies on, attached to THAT edge and pinned by hash;
  * **the dependency register**: each edge with its role, criticality,
    contribution (and the sealed separability claim), contract reference,
    assurance basis (`tested` inside Brutor, `assumed` at the boundary) and
    supplier. The documentation export's DC3 "Components of the system" and
    the assurance report's "Composition and dependencies" section both
    render it, and the obligation registry's resolvers
    (`dependencies.*`) count over it.

Nothing here decides a tier: the effective tier is
`ai_system_composition.propagate_effective_tiers`, read through
`load_tier_facts`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.database.models.ai_system_dependency import (
    ROLE_EXTERNAL,
    ROLE_SHARED_SERVICE,
    AISystemDependency,
)
from app.database.models.ai_system_evidence import (
    EVIDENCE_SUPPLIER_INFORMATION,
    AISystemEvidence,
)
from app.services.ai_system_dependency_health import BASIS_ASSUMED, BASIS_TESTED


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def supplier_required(edge: AISystemDependency) -> bool:
    """Does this edge need supplier information on file (Art 25(4))?

    Every external dependency (a third party by definition), and a shared
    service owned by another legal entity (`supplier_legal_entity` set).
    A shared service of the same legal entity is inside the deployer — its
    evidence is its own segments, not an agreement.
    """
    return edge.role == ROLE_EXTERNAL or bool((edge.supplier_legal_entity or "").strip())


def assurance_basis(edge: AISystemDependency) -> str:
    """`tested` — observed and graded inside Brutor (a shared service);
    `assumed` — seen only at the boundary (an external agent)."""
    return BASIS_ASSUMED if edge.role == ROLE_EXTERNAL else BASIS_TESTED


def supplier_evidence(
    db: Session, tenant_id: str, edge_ids: Iterable[str]
) -> Dict[str, List[AISystemEvidence]]:
    """`{edge id: [supplier_information pointers attached to it]}`."""
    ids = sorted({i for i in edge_ids if i})
    if not ids:
        return {}
    out: Dict[str, List[AISystemEvidence]] = {}
    for row in (
        db.query(AISystemEvidence)
        .filter(
            AISystemEvidence.tenant_id == tenant_id,
            AISystemEvidence.kind == EVIDENCE_SUPPLIER_INFORMATION,
            AISystemEvidence.dependency_id.in_(ids),
        )
        .order_by(AISystemEvidence.dependency_id, AISystemEvidence.id)
    ):
        out.setdefault(row.dependency_id, []).append(row)
    return out


def unexpired(rows: Iterable[AISystemEvidence], now: datetime) -> List[AISystemEvidence]:
    return [r for r in rows if r.valid_until is None or _aware(r.valid_until) > now]


def supplier_block(
    edge: AISystemDependency, rows: List[AISystemEvidence], now: datetime
) -> Dict[str, Any]:
    """What the register says about one edge's supplier information."""
    current = unexpired(rows, now)
    return {
        "required": supplier_required(edge),
        "on_file": len(rows),
        "unexpired": len(current),
        "evidence": [
            {
                "id": r.id,
                "title": r.title,
                "sha256": r.sha256,
                "valid_until": r.valid_until.isoformat() if r.valid_until else None,
                "expired": r not in current,
            }
            for r in rows
        ],
    }


def dependency_register(
    db: Session,
    system: Any,
    *,
    now: Optional[datetime] = None,
    edges: Optional[List[AISystemDependency]] = None,
) -> List[Dict[str, Any]]:
    """Every declared dependency of `system`, as compliance documents it.

    Deterministic for the same inputs (sorted by role, target, card; no
    timestamps other than the stored seal and evidence dates), so a
    documentation export over an unchanged register re-hashes identically.
    """
    from app.services.ai_system_closure import dependency_contract
    from app.services.ai_system_composition import _group_names

    now = now or datetime.now(timezone.utc)
    tenant_id = system.tenant_id
    if edges is None:
        edges = (
            db.query(AISystemDependency)
            .filter(
                AISystemDependency.tenant_id == tenant_id,
                AISystemDependency.ai_system_id == system.id,
            )
            .all()
        )
    if not edges:
        return []
    names = _group_names(db, tenant_id, [e.depends_on_system_id for e in edges])
    card_ids = [e.agent_card_id for e in edges if e.agent_card_id]
    card_names: Dict[str, str] = {}
    if card_ids:
        from app.database.models.agent_card import AgentCard

        card_names = {
            cid: name
            for cid, name in db.query(AgentCard.id, AgentCard.name).filter(
                AgentCard.tenant_id == tenant_id, AgentCard.id.in_(card_ids)
            )
        }
    evidence = supplier_evidence(db, tenant_id, [e.id for e in edges])

    out: List[Dict[str, Any]] = []
    for e in edges:
        contract = None
        if e.role == ROLE_SHARED_SERVICE:
            ref = dependency_contract(db, tenant_id, e.depends_on_system_id, e.contract_pin)
            if ref is not None:
                contract = {**ref, "pinned": e.contract_pin is not None}
        contributes = bool(e.contributes_to_decision)
        out.append({
            "edge_id": e.id,
            "role": e.role,
            "depends_on_system_id": e.depends_on_system_id,
            "depends_on_system_name": names.get(e.depends_on_system_id or ""),
            "agent_card_id": e.agent_card_id,
            "agent_card_name": card_names.get(e.agent_card_id or ""),
            "criticality": e.criticality,
            "fallback": e.fallback,
            "contributes_to_decision": contributes,
            "separability": (
                None
                if contributes
                else {
                    "rationale": e.separability_rationale,
                    "record_id": e.separability_record_id,
                    "sealed_at": (
                        _aware(e.separability_sealed_at).isoformat()
                        if e.separability_sealed_at
                        else None
                    ),
                    "sealed": bool(e.separability_record_id),
                }
            ),
            "contract": contract,
            "assurance_basis": assurance_basis(e),
            "assumptions": (e.assumptions or {}) if e.role == ROLE_EXTERNAL else None,
            "supplier_legal_entity": e.supplier_legal_entity,
            "supplier_information": supplier_block(e, evidence.get(e.id, []), now),
        })
    out.sort(key=lambda d: (d["role"], d["depends_on_system_id"] or "", d["agent_card_id"] or "", d["edge_id"]))
    return out


#: The report section's title.
REPORT_SECTION_TITLE = "Composition and dependencies"


def composition_report_section(
    db: Session,
    system: Any,
    dependency_health: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The assurance report's "Composition and dependencies" section.

    Each declared edge from the dependency register, joined with its current
    status from the SAME dependency-health entries the report's health block
    carries (never a second computation), plus the system's place in the
    composition (role, effective tier, callers) and every open composition
    finding. Frozen into the report like every other section, so the
    report's digest and signature cover it.
    """
    from app.services.ai_system_composition import (
        DEFAULT_COMPOSITION_WINDOW_HOURS,
        CompositionIndex,
    )
    from app.services.assurance_checks.composition import composition_findings
    from app.services.ai_system_closure import resolve_closure
    from app.services.ai_system_composition import member_agents

    now = now or datetime.now(timezone.utc)
    index = CompositionIndex(
        db, system.tenant_id, window_hours=DEFAULT_COMPOSITION_WINDOW_HOURS, now=now
    )
    status_by_edge = {d["edge_id"]: d for d in dependency_health}
    register = dependency_register(db, system, now=now, edges=index.edges_by_system.get(system.id, []))
    edges = []
    for d in register:
        health = status_by_edge.get(d["edge_id"]) or {}
        edges.append({
            **d,
            "status": health.get("status"),
            "failing_signal": health.get("failing_signal"),
            "caps_root": bool(health.get("caps_root")),
            "boundary": health.get("boundary"),
        })
    observed_callers = index.observed_callers(system.id)
    accepted = index.accepted_callers(system.id)
    return {
        "title": REPORT_SECTION_TITLE,
        "window_hours": index.window_hours,
        "role": index.role(system.id),
        "risk_tier_declared": getattr(system, "eu_ai_act_risk_tier", None),
        "risk_tier_effective": index.risk_tier_effective(system.id),
        "effective_tier_sources": index.effective_tier_sources(system.id),
        "separability_declarations": index.separability_declarations(system.id),
        "enforce_accepted_callers": index.enforces_accepted_callers(system.id),
        "edges": edges,
        "callers": [
            {
                "system_id": caller,
                "system_name": index.system_name(caller),
                "accepted": caller in accepted,
                "observed_calls": observed_callers[caller].calls if caller in observed_callers else 0,
            }
            for caller in sorted(accepted | set(observed_callers))
        ],
        "findings": composition_findings(index, system.id),
        # Each member agent's CURRENT
        # release with its trust (verified | declared) and approval (derived
        # from the identity's approved versions; null = observe only), and
        # the agent findings on this system. Frozen with the report, so an
        # unapproved release or an undeclared change is sealed with it.
        "agents": member_agents(
            db, system.tenant_id,
            (resolve_closure(db, system).get("composition") or {}).get("agent_identities") or [],
        ),
        "agent_findings": _report_agent_findings(db, system, now),
    }


def _report_agent_findings(db: Session, system: Any, now: datetime) -> List[Dict[str, Any]]:
    from app.services.assurance_checks.agent import (
        agent_findings,
        findings_for_system,
        load_agent_facts,
    )

    facts = load_agent_facts(db, system.tenant_id, now=now)
    return [
        {k: f[k] for k in ("code", "severity", "agent_id", "agent_name", "release", "message")}
        for f in findings_for_system(agent_findings(db, facts), system.id)
    ]


__all__ = [
    "REPORT_SECTION_TITLE",
    "composition_report_section",
    "BASIS_ASSUMED",
    "BASIS_TESTED",
    "assurance_basis",
    "dependency_register",
    "supplier_block",
    "supplier_evidence",
    "supplier_required",
    "unexpired",
]
