# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems admin API

    GET    /v1/admin/ai-systems/{id}/dependencies             — declared edges
    POST   /v1/admin/ai-systems/{id}/dependencies             — declare one
    PATCH  /v1/admin/ai-systems/{id}/dependencies/{edge_id}   — edit (merged edge re-validated)
    DELETE /v1/admin/ai-systems/{id}/dependencies/{edge_id}   — remove
    GET    /v1/admin/ai-systems/{id}/composition              — the Composition tab

The composition view is built on the contract closure (`resolve_closure`) and
the tenant composition index (`app/services/ai_system_composition.py`); this
module only maps it onto the response models.

422 on a rule violation (self-edge, missing target, degradable without a
fallback, non-contribution without a rationale, a pin that is not a contract
of the target, an external edge without its supplier_legal_entity, an
assumptions object outside the strict schema, …); 409 on a duplicate edge.

A non-contributing edge's separability claim is SEALED as
an `ai.brutor.declaration` after the save commits (record id/at on the edge;
`separability_seal` on the write response says what THIS save sealed). A
changed or withdrawn claim clears the old record; an unsealable one is saved
unsealed (capture-gap item) and sealed on a later save.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.endpoints.admin._compliance_http import seal_status
from app.api.v1.models.admin.ai_system_composition import (
    CompositionResponse,
    DependencyCreate,
    DependencyEdge,
    DependencyListResponse,
    DependencyUpdate,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_mixins import actor_name
from app.services.ai_system_composition import (
    DEFAULT_COMPOSITION_WINDOW_HOURS,
    CompositionIndex,
    DependencyConflictError,
    DependencyValidationError,
    build_composition,
    dependency_edge_dict,
    load_ai_system,
    validate_dependency,
)
from app.core.sync_bridge import block_on
from app.services.cache_invalidation import (
    INVALIDATION_AI_SYSTEM_DEPENDENCY,
    invalidate_proxy_cache,
)
from app.services.compliance import evaluation
from app.services.compliance.sealing import SealOutcome
from app.services.compliance.sealing_hooks import seal_separability

_EDGE_FIELDS = (
    "role", "depends_on_system_id", "agent_card_id", "criticality", "fallback",
    "contributes_to_decision", "separability_rationale", "contract_pin", "assumptions",
    "supplier_legal_entity", "expected_outcomes",
)

#: The fields a separability declaration seals (see
#: `sealing_hooks.separability_payload`): a change to any of them is a new
#: claim, so the old record no longer describes the edge.
_SEPARABILITY_CLAIM_FIELDS = (
    "depends_on_system_id", "agent_card_id", "contributes_to_decision",
    "separability_rationale",
)


def _system_or_http(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
    try:
        return load_ai_system(db, tenant_id, ai_system_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _edge_or_404(db: Session, tenant_id: str, ai_system_id: str, edge_id: str) -> AISystemDependency:
    edge = (
        db.query(AISystemDependency)
        .filter(
            AISystemDependency.id == edge_id,
            AISystemDependency.tenant_id == tenant_id,
            AISystemDependency.ai_system_id == ai_system_id,
        )
        .first()
    )
    if edge is None:
        raise HTTPException(status_code=404, detail=f"Dependency {edge_id!r} not found")
    return edge


def _validated(db: Session, tenant_id: str, ai_system_id: str, fields: Dict[str, Any],
               edge_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        return validate_dependency(
            db, tenant_id=tenant_id, ai_system_id=ai_system_id, fields=fields, edge_id=edge_id
        )
    except DependencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DependencyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _commit_or_409(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        # The unique constraints are the backstop for a concurrent declare
        # that passed the pre-check.
        db.rollback()
        raise HTTPException(
            status_code=409, detail="An equivalent dependency is already declared"
        ) from exc


def _edge_response(db: Session, tenant_id: str, edge: AISystemDependency,
                   seal: Optional[SealOutcome] = None) -> DependencyEdge:
    return DependencyEdge(
        **dependency_edge_dict(edge, _Names(db, tenant_id)),
        separability_seal=seal_status(seal) if seal is not None else None,
    )


def _seal_separability_if_claimed(db: Session, edge: AISystemDependency) -> Optional[SealOutcome]:
    """A separability claim is SEALED like every other
    compliance declaration. Called after the edge is committed.

    A non-contributing edge without a current record is sealed now (a new
    claim, a changed claim, or a claim whose earlier seal failed); a
    contributing edge carries no claim. A seal the core cannot make leaves
    the record id NULL — the edge is saved, the registry does not accept an
    unsealed claim, and `_seal` raised a capture-gap item.
    """
    if edge.contributes_to_decision or edge.separability_record_id:
        return None
    outcome = seal_separability(db, edge)
    if outcome.sealed:
        edge.separability_record_id = outcome.record_id
        edge.separability_sealed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(edge)
    evaluation.clear_cache(edge.tenant_id)
    return outcome


class _Names:
    """The two name lookups `dependency_edge_dict` needs, without building a
    whole composition index for one edge."""

    def __init__(self, db: Session, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    def system_name(self, sid: Optional[str]) -> Optional[str]:
        if not sid:
            return None
        row = (
            self._db.query(ResourceGroup.name, ResourceGroup.display_name)
            .filter(ResourceGroup.id == sid, ResourceGroup.tenant_id == self._tenant_id)
            .first()
        )
        return (row[1] or row[0]) if row else None

    def card_name(self, cid: Optional[str]) -> Optional[str]:
        if not cid:
            return None
        from app.database.models.agent_card import AgentCard

        row = (
            self._db.query(AgentCard.name)
            .filter(AgentCard.id == cid, AgentCard.tenant_id == self._tenant_id)
            .first()
        )
        return row[0] if row else None


def create_ai_system_composition_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/ai-systems", tags=["AI System Composition"])

    @router.get(
        "/{ai_system_id}/dependencies",
        response_model=DependencyListResponse,
        summary="Declared dependencies of an AI System",
    )
    def list_dependencies(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> DependencyListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _system_or_http(db, resolved, ai_system_id)
        rows = (
            db.query(AISystemDependency)
            .filter(
                AISystemDependency.tenant_id == resolved,
                AISystemDependency.ai_system_id == ai_system_id,
            )
            .order_by(AISystemDependency.declared_at, AISystemDependency.id)
            .all()
        )
        names = _Names(db, resolved)
        return DependencyListResponse(
            dependencies=[DependencyEdge(**dependency_edge_dict(r, names)) for r in rows],
            total=len(rows),
        )

    @router.post(
        "/{ai_system_id}/dependencies",
        response_model=DependencyEdge,
        status_code=201,
        summary="Declare a dependency",
    )
    def create_dependency(
        request: Request,
        ai_system_id: str,
        payload: DependencyCreate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DependencyEdge:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _system_or_http(db, resolved, ai_system_id)

        fields = _validated(db, resolved, ai_system_id, payload.model_dump())
        who = actor_name(admin)
        edge = AISystemDependency(
            id=f"dep-{ULID()}",
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            declared_by=who,
            updated_by=who,
            **{k: fields[k] for k in _EDGE_FIELDS},
        )
        db.add(edge)
        _commit_or_409(db)
        db.refresh(edge)
        # The core derives the tenant's card → service map from declared
        # edges (accepted-caller enforcement): tell the proxies.
        block_on(invalidate_proxy_cache(
            request, INVALIDATION_AI_SYSTEM_DEPENDENCY, resolved, edge.id, action="create"
        ))
        seal = _seal_separability_if_claimed(db, edge)
        evaluation.clear_cache(resolved)
        logger.info(
            "AI System dependency declared",
            context={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "dependency_id": edge.id,
                "role": edge.role,
            },
        )
        return _edge_response(db, resolved, edge, seal)

    @router.patch(
        "/{ai_system_id}/dependencies/{edge_id}",
        response_model=DependencyEdge,
        summary="Edit a dependency",
    )
    def update_dependency(
        request: Request,
        ai_system_id: str,
        edge_id: str,
        payload: DependencyUpdate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DependencyEdge:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _system_or_http(db, resolved, ai_system_id)
        edge = _edge_or_404(db, resolved, ai_system_id, edge_id)

        merged = {k: getattr(edge, k) for k in _EDGE_FIELDS}
        merged.update(payload.model_dump(exclude_unset=True))
        fields = _validated(db, resolved, ai_system_id, merged, edge_id=edge.id)

        changed = False
        claim_changed = False
        for key in _EDGE_FIELDS:
            if getattr(edge, key) != fields[key]:
                setattr(edge, key, fields[key])
                changed = True
                claim_changed = claim_changed or key in _SEPARABILITY_CLAIM_FIELDS
        if changed:
            # Only a real change names a new editor — a no-op save must not
            # claim someone edited the edge.
            edge.updated_by = actor_name(admin)
        if claim_changed or edge.contributes_to_decision:
            # The sealed record described the OLD claim (or there is no claim
            # any more): a stale id would cite a declaration that no longer
            # matches the edge.
            edge.separability_record_id = None
            edge.separability_sealed_at = None
        _commit_or_409(db)
        db.refresh(edge)
        if changed:
            # The core derives the tenant's card → service map from declared
            # edges (accepted-caller enforcement): tell the proxies.
            block_on(invalidate_proxy_cache(
                request, INVALIDATION_AI_SYSTEM_DEPENDENCY, resolved, edge.id, action="update"
            ))
        seal = _seal_separability_if_claimed(db, edge)
        evaluation.clear_cache(resolved)
        return _edge_response(db, resolved, edge, seal)

    @router.delete(
        "/{ai_system_id}/dependencies/{edge_id}",
        status_code=204,
        response_model=None,
        summary="Remove a dependency",
    )
    def delete_dependency(
        request: Request,
        ai_system_id: str,
        edge_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> None:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _system_or_http(db, resolved, ai_system_id)
        edge = _edge_or_404(db, resolved, ai_system_id, edge_id)
        db.delete(edge)
        db.commit()
        # The core derives the tenant's card → service map from declared
        # edges (accepted-caller enforcement): tell the proxies.
        block_on(invalidate_proxy_cache(
            request, INVALIDATION_AI_SYSTEM_DEPENDENCY, resolved, edge_id, action="delete"
        ))
        evaluation.clear_cache(resolved)
        logger.info(
            "AI System dependency removed",
            context={"tenant_id": resolved, "ai_system_id": ai_system_id, "dependency_id": edge_id},
        )

    @router.get(
        "/{ai_system_id}/composition",
        response_model=CompositionResponse,
        summary="Composition of an AI System (declared + observed)",
        description=(
            "Location, member agents, resources and gateway policies/limits "
            "(from the contract closure, with inheritance), declared "
            "dependencies joined with observed run segments, dependents, "
            "client labels and composition findings."
        ),
    )
    def get_composition(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        window_hours: int = Query(DEFAULT_COMPOSITION_WINDOW_HOURS, ge=1, le=24 * 366),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> CompositionResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _system_or_http(db, resolved, ai_system_id)
        index = CompositionIndex(db, resolved, window_hours=window_hours)
        return CompositionResponse(
            **build_composition(db, system, window_hours=window_hours, index=index)
        )

    return router
