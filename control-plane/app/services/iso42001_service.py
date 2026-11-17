# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
ISO/IEC 42001 AIMS service (Design Note 001 + iso-42001 plan). Produces the
control-coverage / Statement of Applicability (SoA), the readiness rollup, and
the auditor-facing evidence pack. The AI System registry is the "AI system
inventory" centerpiece; audit is the operational evidence.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.discovery import DiscoveredAsset
from app.database.models.guardrail_config import GuardrailConfig
from app.database.models.iso42001 import (
    COVERAGE_IMPLEMENTED,
    COVERAGE_NOT_IMPLEMENTED,
    COVERAGE_PARTIALLY_IMPLEMENTED,
    COVERAGE_SATISFIED,
    COVERAGE_STATUSES,
    Iso42001Control,
    Iso42001ControlCoverage,
)
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.mcp_server import MCPServer
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.semantic_policy import SemanticPolicy
from app.database.models.usage_log import UsageLog


# Provenance of a control's coverage status in the SoA response.
SOURCE_MANUAL = "manual"    # operator set an explicit coverage row
SOURCE_AUTO = "auto"        # derived live from real Brutor data
SOURCE_DEFAULT = "default"  # static default from the control's brutor_capability


def _exists(db: Session, model: Any, *filters: Any) -> bool:
    """Fail-safe EXISTS. Any DB error → False (a control never auto-satisfies
    on an error; the operator can still set it manually)."""
    try:
        return db.query(model).filter(*filters).limit(1).first() is not None
    except Exception:  # noqa: BLE001 — advisory readiness must never 500
        return False


# Live auto-derivation for the "covered" controls: each returns True when the
# real Brutor data that evidences the control actually exists for the tenant, so
# the SoA reflects what THIS tenant has done — not just what the platform can do.
def _derive_resource_documentation(db: Session, t: str) -> bool:  # A.4.2
    return (
        _exists(
            db, ResourceGroup,
            ResourceGroup.tenant_id == t,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        or _exists(db, LLModel, LLModel.tenant_id == t)
        or _exists(db, MCPServer, MCPServer.tenant_id == t)
        or _exists(db, DiscoveredAsset, DiscoveredAsset.tenant_id == t)
    )


def _enforcement_configured(db: Session, t: str) -> bool:
    return _exists(
        db, GuardrailConfig, GuardrailConfig.tenant_id == t, GuardrailConfig.enabled.is_(True)
    ) or _exists(
        db, SemanticPolicy, SemanticPolicy.tenant_id == t, SemanticPolicy.enabled.is_(True)
    )


def _event_logs_recorded(db: Session, t: str) -> bool:  # A.6.2.8
    return _exists(db, ProxyLog, ProxyLog.tenant_id == t) or _exists(
        db, UsageLog, UsageLog.tenant_id == t
    )


def _operation_and_monitoring(db: Session, t: str) -> bool:  # A.6.2.6
    # Traffic is being monitored AND runtime enforcement is configured.
    return _event_logs_recorded(db, t) and _enforcement_configured(db, t)


def _post_market_monitoring(db: Session, t: str) -> bool:  # A.6.2.7
    """Continuous post-market monitoring evidence.

    The clause wants monitoring that keeps producing evidence, not a review
    somebody did once. Satisfied when an AI System has a captured assurance
    report: that is a dated artifact recording what the system was allowed
    to do, what it did, and what the platform could not see.
    """
    from app.database.models.ai_system_health import AISystemAssuranceReport

    return _exists(db, AISystemAssuranceReport, AISystemAssuranceReport.tenant_id == t)


def _performance_evaluation(db: Session, t: str) -> bool:  # A.6.2.4
    """Behavioural baselines exist, so "performing as expected" is measurable.

    Deliberately keyed on a *ready* baseline rather than on the feature being
    switched on: a system still learning its normal cannot yet evidence that
    anything is being evaluated against it.
    """
    from app.database.models.ai_system_drift import AISystemBaseline, BASELINE_READY

    return _exists(
        db,
        AISystemBaseline,
        AISystemBaseline.tenant_id == t,
        AISystemBaseline.state == BASELINE_READY,
    )


def _suppliers_registered(db: Session, t: str) -> bool:  # A.10.3
    return _exists(db, LlmProvider, LlmProvider.tenant_id == t)


# control_id → live checker (only the "covered" controls have one).
_AUTO_DERIVERS = {
    "A.4.2": _derive_resource_documentation,
    "A.6.2.6": _operation_and_monitoring,
    "A.6.2.8": _event_logs_recorded,
    "A.9.2": _enforcement_configured,   # processes for responsible use
    "A.10.3": _suppliers_registered,
    # The lifecycle-assurance controls.
    "A.6.2.4": _performance_evaluation,
    "A.6.2.7": _post_market_monitoring,
}


class Iso42001Error(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# Default coverage derived from what Brutor supplies out of the box, so a fresh
# tenant's SoA already reflects the platform's contribution (operator refines).
_CAPABILITY_DEFAULT_STATUS = {
    "covered": COVERAGE_IMPLEMENTED,
    "partial": COVERAGE_PARTIALLY_IMPLEMENTED,
    "org": COVERAGE_NOT_IMPLEMENTED,
}


def get_coverage(db: Session, tenant_id: str) -> Dict[str, Any]:
    """The Statement of Applicability: every Annex A control × the tenant's
    status/owner/evidence, with a readiness rollup. Where the tenant has no
    explicit coverage row, the status defaults from Brutor's capability."""
    controls = (
        db.query(Iso42001Control).order_by(Iso42001Control.sort_order).all()
    )
    coverage_rows = {
        c.control_id: c
        for c in db.query(Iso42001ControlCoverage).filter(
            Iso42001ControlCoverage.tenant_id == tenant_id
        )
    }

    items: List[Dict[str, Any]] = []
    per_clause: Dict[str, Dict[str, int]] = {}
    satisfied = 0
    for ctrl in controls:
        row = coverage_rows.get(ctrl.control_id)
        if row is not None:
            # Operator-set coverage always wins.
            status = row.status
            owner = row.owner
            evidence = row.evidence
            notes = row.notes
            explicit = True
            source = SOURCE_MANUAL
        else:
            deriver = _AUTO_DERIVERS.get(ctrl.control_id)
            if deriver is not None:
                # Live-derived from real tenant data (only flips to satisfied
                # when the evidencing data actually exists for this tenant).
                status = (
                    COVERAGE_IMPLEMENTED if deriver(db, tenant_id)
                    else COVERAGE_NOT_IMPLEMENTED
                )
                source = SOURCE_AUTO
            else:
                status = _CAPABILITY_DEFAULT_STATUS.get(
                    ctrl.brutor_capability, COVERAGE_NOT_IMPLEMENTED
                )
                source = SOURCE_DEFAULT
            owner = None
            evidence = None
            notes = None
            explicit = False
        if status in COVERAGE_SATISFIED:
            satisfied += 1
        clause = per_clause.setdefault(
            ctrl.clause, {"total": 0, "satisfied": 0}
        )
        clause["total"] += 1
        if status in COVERAGE_SATISFIED:
            clause["satisfied"] += 1
        items.append(
            {
                **ctrl.to_dict(),
                "status": status,
                "owner": owner,
                "evidence": evidence,
                "notes": notes,
                "explicit": explicit,
                "source": source,
            }
        )

    total = len(controls)
    return {
        "readiness_percent": round(100.0 * satisfied / total, 1) if total else 0.0,
        "controls_total": total,
        "controls_satisfied": satisfied,
        "per_clause": [
            {
                "clause": cl,
                "total": v["total"],
                "satisfied": v["satisfied"],
                "readiness_percent": round(100.0 * v["satisfied"] / v["total"], 1),
            }
            for cl, v in sorted(per_clause.items())
        ],
        "controls": items,
    }


def set_coverage(
    db: Session,
    tenant_id: str,
    control_id: str,
    *,
    status: str,
    owner: Optional[str] = None,
    evidence: Any = None,
    notes: Optional[str] = None,
    next_review: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> Iso42001ControlCoverage:
    if status not in COVERAGE_STATUSES:
        raise Iso42001Error(
            f"Invalid status {status!r}; expected one of {sorted(COVERAGE_STATUSES)}"
        )
    if db.get(Iso42001Control, control_id) is None:
        raise Iso42001Error(f"Unknown control {control_id!r}", 404)
    row = (
        db.query(Iso42001ControlCoverage)
        .filter(
            Iso42001ControlCoverage.tenant_id == tenant_id,
            Iso42001ControlCoverage.control_id == control_id,
        )
        .first()
    )
    parsed_review = None
    if next_review:
        parsed_review = _dt.datetime.fromisoformat(next_review)
    if row is None:
        row = Iso42001ControlCoverage(
            id=f"iso42cov-{ULID()}",
            tenant_id=tenant_id,
            control_id=control_id,
            status=status,
            owner=owner,
            evidence=evidence,
            notes=notes,
            last_reviewed=_dt.datetime.now(_dt.timezone.utc),
            next_review=parsed_review,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.status = status
        if owner is not None:
            row.owner = owner
        if evidence is not None:
            row.evidence = evidence
        if notes is not None:
            row.notes = notes
        if parsed_review is not None:
            row.next_review = parsed_review
        row.last_reviewed = _dt.datetime.now(_dt.timezone.utc)
        row.updated_by = updated_by
    db.flush()
    return row


ISO_FRAMEWORK_ID = "iso-42001"


def annex_a_statements(
    db: Session,
    tenant_id: str,
    from_dt: _dt.datetime,
    to_dt: _dt.datetime,
    *,
    include: bool = True,
) -> Dict[str, Any]:
    """The ISO/IEC 42001 Annex A statements over a window,
    evaluated through the shared resolvers (estate-wide). The activity
    endpoint and the evidence pack cite these; the SoA above stays the
    operator's own declaration."""
    if not include:
        return {"available": False, "framework_id": ISO_FRAMEWORK_ID, "reason": "not requested"}
    from app.services.compliance.framework_statements import statements

    return statements(db, tenant_id, ISO_FRAMEWORK_ID, from_dt, to_dt)


def evidence_pack(db: Session, tenant_id: str, *, generated_at: str) -> Dict[str, Any]:
    """Assemble the auditor-facing ISO 42001 evidence bundle: the AI system
    inventory (centerpiece), the SoA/coverage, and the governing controls.

    Design Note 002: an AI System is a Resource Group with group_type='ai_system', so
    the inventory is those RGs (no separate ai_systems table)."""
    coverage = get_coverage(db, tenant_id)
    systems = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        .order_by(ResourceGroup.name)
        .all()
    )
    return {
        "standard": "ISO/IEC 42001:2023",
        "tenant_id": tenant_id,
        "generated_at": generated_at,
        "readiness_percent": coverage["readiness_percent"],
        "ai_system_inventory": [s.to_dict() for s in systems],
        "ai_system_count": len(systems),
        "statement_of_applicability": coverage["controls"],
        "per_clause_readiness": coverage["per_clause"],
        # The Annex A evidence statements over the last 30 days.
        "annex_a_statements": _pack_statements(db, tenant_id),
    }


def _pack_statements(db: Session, tenant_id: str) -> Dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc)
    return annex_a_statements(db, tenant_id, now - _dt.timedelta(days=30), now)
