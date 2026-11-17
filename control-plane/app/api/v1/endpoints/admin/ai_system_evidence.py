# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System evidence attachments

    GET    /v1/admin/ai-systems/{id}/evidence                — list artifacts
    POST   /v1/admin/ai-systems/{id}/evidence                — attach one
    PATCH  /v1/admin/ai-systems/{id}/evidence/{evidence_id}  — update metadata
    DELETE /v1/admin/ai-systems/{id}/evidence/{evidence_id}  — detach

A row is a **pointer with a fingerprint**, not a document: the artifact
stays in the organisation's document system, and the platform records
where it lives, its SHA-256, who produced it and whether that party was
independent of the team that built the system.

A `supplier_information` pointer (EU AI Act Art 25(4)) is
about ONE dependency edge — the written agreement the system relies on from
the supplier of that dependency — so it names the edge (`dependency_id`, an
edge of this system) and is pinned by hash (`sha256` required). No other
kind names an edge.

Two consumers read these rows:

* the lifecycle gate — high-risk systems need an unexpired
  `impact_assessment` before `proposed → approved`;
* the Assurance Report — an `evidence` section, and honest `not_covered`
  lines when nothing (or nothing independent) is attached.

Deletion is allowed — a mis-attached artifact must be removable — and is
logged. What deletion cannot do is rewrite history: captured assurance
snapshots are immutable, so a report that listed the artifact keeps
listing it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_evidence import (
    EDGE_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    AISystemEvidence,
)
from app.database.models.resource_group import GroupType, ResourceGroup


class EvidenceResponse(BaseModel):
    id: str
    ai_system_id: str
    kind: str
    title: str
    notes: Optional[str] = None
    uri: Optional[str] = None
    sha256: Optional[str] = None
    assessor: Optional[str] = None
    independent: Optional[bool] = None
    produced_at: Optional[str] = None
    valid_until: Optional[str] = None
    dependency_id: Optional[str] = Field(
        None, description="The dependency edge a supplier_information pointer is attached to"
    )
    is_expired: bool = False
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EvidenceListResponse(BaseModel):
    evidence: List[EvidenceResponse]
    total: int


class EvidenceCreateRequest(BaseModel):
    kind: str = Field(..., description=f"One of {sorted(EVIDENCE_KINDS)}")
    title: str = Field(..., min_length=1, max_length=500)
    notes: Optional[str] = Field(None, max_length=10_000)
    uri: Optional[str] = Field(None, max_length=4000)
    sha256: Optional[str] = Field(
        None,
        description="SHA-256 of the artifact bytes, 64 hex chars — lets a "
        "reviewer verify the document at `uri` is the one this row vouches for",
    )
    assessor: Optional[str] = Field(None, max_length=255)
    independent: Optional[bool] = Field(
        None,
        description="Produced by a party separate from the team that built "
        "or operates the system. Omit when unknown — that is a different "
        "claim from 'self-assessed'.",
    )
    produced_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    dependency_id: Optional[str] = Field(
        None,
        description=(
            "Required for kind supplier_information (and only for it): the id of one of "
            "this system's dependency edges the agreement covers."
        ),
    )


class EvidenceUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    notes: Optional[str] = Field(None, max_length=10_000)
    uri: Optional[str] = Field(None, max_length=4000)
    sha256: Optional[str] = None
    assessor: Optional[str] = Field(None, max_length=255)
    independent: Optional[bool] = None
    produced_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    dependency_id: Optional[str] = None


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def create_ai_system_evidence_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Evidence"])

    def _load_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == ai_system_id,
                ResourceGroup.tenant_id == tenant_id,
            )
            .first()
        )
        if system is None:
            raise HTTPException(
                status_code=404, detail=f"AI System {ai_system_id!r} not found"
            )
        if system.group_type != GroupType.AI_SYSTEM:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{ai_system_id!r} is a {system.group_type} group, not an "
                    f"AI System — evidence attaches to a governed workload, "
                    f"not an access group"
                ),
            )
        return system

    def _load_evidence(
        db: Session, tenant_id: str, ai_system_id: str, evidence_id: str
    ) -> AISystemEvidence:
        row = (
            db.query(AISystemEvidence)
            .filter(
                AISystemEvidence.id == evidence_id,
                AISystemEvidence.tenant_id == tenant_id,
                AISystemEvidence.ai_system_id == ai_system_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Evidence {evidence_id!r} not found"
            )
        return row

    def _validate_edge_link(
        db: Session, tenant_id: str, ai_system_id: str, kind: str,
        dependency_id: Optional[str], sha256: Optional[str],
    ) -> None:
        """An edge-scoped kind names one of THIS system's edges and is pinned
        by hash; every other kind names no edge."""
        if kind not in EDGE_EVIDENCE_KINDS:
            if dependency_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"dependency_id applies to {sorted(EDGE_EVIDENCE_KINDS)} evidence only",
                )
            return
        if not dependency_id:
            raise HTTPException(
                status_code=422,
                detail=f"{kind} evidence requires dependency_id: the dependency edge it covers",
            )
        edge = (
            db.query(AISystemDependency.id)
            .filter(
                AISystemDependency.id == dependency_id,
                AISystemDependency.tenant_id == tenant_id,
                AISystemDependency.ai_system_id == ai_system_id,
            )
            .first()
        )
        if edge is None:
            raise HTTPException(
                status_code=422,
                detail=f"dependency_id {dependency_id!r} is not a dependency of {ai_system_id!r}",
            )
        if not sha256:
            raise HTTPException(
                status_code=422,
                detail=f"{kind} evidence is pinned by hash: sha256 is required",
            )

    def _validate_dates(
        produced_at: Optional[datetime], valid_until: Optional[datetime]
    ) -> None:
        if produced_at and valid_until and valid_until <= produced_at:
            raise HTTPException(
                status_code=422,
                detail="valid_until must be after produced_at",
            )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/evidence",
        response_model=EvidenceListResponse,
        summary="External evidence artifacts attached to an AI System",
    )
    def list_evidence(
        ai_system_id: str,
        kind: Optional[str] = Query(
            None, description=f"Filter by kind — one of {sorted(EVIDENCE_KINDS)}"
        ),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> EvidenceListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        if kind is not None and kind not in EVIDENCE_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {sorted(EVIDENCE_KINDS)}",
            )

        q = db.query(AISystemEvidence).filter(
            AISystemEvidence.tenant_id == resolved,
            AISystemEvidence.ai_system_id == ai_system_id,
        )
        if kind is not None:
            q = q.filter(AISystemEvidence.kind == kind)
        rows = q.order_by(AISystemEvidence.created_at.desc()).all()

        return EvidenceListResponse(
            evidence=[_response(r) for r in rows],
            total=len(rows),
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/evidence",
        response_model=EvidenceResponse,
        status_code=201,
        summary="Attach an evidence artifact",
    )
    def create_evidence(
        ai_system_id: str,
        payload: EvidenceCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> EvidenceResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        _validate_dates(payload.produced_at, payload.valid_until)
        if payload.kind in EVIDENCE_KINDS:
            _validate_edge_link(
                db, resolved, ai_system_id, payload.kind, payload.dependency_id, payload.sha256
            )

        row = AISystemEvidence(
            id=f"ev-{ULID()}",
            tenant_id=resolved,
            ai_system_id=ai_system_id,
        )
        try:
            row.kind = payload.kind
            row.sha256 = payload.sha256
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        row.title = payload.title
        row.notes = payload.notes
        row.uri = payload.uri
        row.assessor = payload.assessor
        row.independent = payload.independent
        row.produced_at = payload.produced_at
        row.valid_until = payload.valid_until
        row.dependency_id = payload.dependency_id

        db.add(row)
        db.commit()
        db.refresh(row)

        logger.info(
            "AI System evidence attached",
            extra={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "evidence_id": row.id,
                "kind": row.kind,
                "actor": getattr(admin, "username", None),
            },
        )
        return _response(row)

    @router.patch(
        "/admin/ai-systems/{ai_system_id}/evidence/{evidence_id}",
        response_model=EvidenceResponse,
        summary="Update an evidence artifact's metadata",
    )
    def update_evidence(
        ai_system_id: str,
        evidence_id: str,
        payload: EvidenceUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> EvidenceResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        row = _load_evidence(db, resolved, ai_system_id, evidence_id)

        data = payload.model_dump(exclude_unset=True)
        produced = data.get("produced_at", row.produced_at)
        until = data.get("valid_until", row.valid_until)
        _validate_dates(produced, until)
        if "dependency_id" in data or "sha256" in data:
            _validate_edge_link(
                db, resolved, ai_system_id, row.kind,
                data.get("dependency_id", row.dependency_id), data.get("sha256", row.sha256),
            )

        try:
            for field_name, value in data.items():
                setattr(row, field_name, value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        db.commit()
        db.refresh(row)
        return _response(row)

    @router.delete(
        "/admin/ai-systems/{ai_system_id}/evidence/{evidence_id}",
        summary="Detach an evidence artifact",
    )
    def delete_evidence(
        ai_system_id: str,
        evidence_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        row = _load_evidence(db, resolved, ai_system_id, evidence_id)

        db.delete(row)
        db.commit()

        logger.info(
            "AI System evidence detached",
            extra={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "evidence_id": evidence_id,
                "kind": row.kind,
                "title": row.title,
                "actor": getattr(admin, "username", None),
            },
        )
        return {"deleted": True, "id": evidence_id}

    def _response(row: AISystemEvidence) -> EvidenceResponse:
        data = row.to_dict()
        data.pop("tenant_id", None)
        return EvidenceResponse(**data)

    return router
