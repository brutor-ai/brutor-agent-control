# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Liveness admin API

    GET  /v1/admin/ai-systems/{id}/liveness           — current expectation + status
    PUT  /v1/admin/ai-systems/{id}/liveness           — set it
    POST /v1/admin/ai-systems/{id}/liveness/learn     — propose from observed cadence
    POST /v1/admin/ai-systems/{id}/liveness/accept    — take the proposal as-is

Detection itself runs in the Rust core: it has to fire on the *absence* of
traffic, and every periodic loop in the platform lives there with an
operator-console beacon. This surface is configuration and status only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.admin.ai_system_liveness import (
    CadenceProposalResponse,
    LivenessExpectationResponse,
    LivenessExpectationUpdate,
    LivenessStatusResponse,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_liveness import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_WINDOW_SECONDS,
    LIVENESS_MODES,
    AISystemLivenessExpectation,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_liveness_state import (
    DEFAULT_LIVENESS_WINDOW_HOURS,
    liveness_state,
)
from app.services.liveness_learning import (
    default_mode_for,
    liveness_learning_service,
)


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _require_ai_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
    system = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.id == ai_system_id,
            ResourceGroup.tenant_id == tenant_id,
        )
        .first()
    )
    if system is None:
        raise HTTPException(status_code=404, detail=f"AI System {ai_system_id!r} not found")
    if system.group_type != GroupType.AI_SYSTEM:
        # A liveness expectation on a plain resource group would never be
        # evaluated — the detector only walks AI Systems — so accepting it
        # would silently do nothing.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Resource group {ai_system_id!r} is not an AI System "
                f"(group_type='{system.group_type.value if system.group_type else None}')"
            ),
        )
    return system


def create_ai_system_liveness_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/ai-systems", tags=["AI System Liveness"])

    def _get_or_default(
        db: Session, tenant_id: str, system: ResourceGroup
    ) -> AISystemLivenessExpectation:
        """The stored expectation, or an unsaved one carrying the
        kind-derived default so a GET never 404s on 'not configured yet'."""
        existing = (
            db.query(AISystemLivenessExpectation)
            .filter(
                AISystemLivenessExpectation.tenant_id == tenant_id,
                AISystemLivenessExpectation.ai_system_id == system.id,
            )
            .first()
        )
        if existing:
            return existing
        return AISystemLivenessExpectation(
            id="",  # unsaved
            tenant_id=tenant_id,
            ai_system_id=system.id,
            mode=default_mode_for(system.system_kind),
            expected_min_runs=1,
            window_seconds=DEFAULT_WINDOW_SECONDS,
            grace_seconds=DEFAULT_GRACE_SECONDS,
            enabled=True,
            accepted_from_learned=False,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @router.get(
        "/{ai_system_id}/liveness",
        response_model=LivenessStatusResponse,
        summary="Liveness expectation and current status",
    )
    def get_liveness(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        window_hours: int = Query(
            DEFAULT_LIVENESS_WINDOW_HOURS, ge=1, le=24 * 366,
            description="Judgment window (defaults to the health scorer's)",
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> LivenessStatusResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _require_ai_system(db, resolved, ai_system_id)

        expectation = _get_or_default(db, resolved, system)

        # The verdict comes from the ONE liveness function the health scorer
        # and the AI Systems list also read, so every surface shows the same
        # state.
        verdict = liveness_state(db, system, window_hours=window_hours)

        return LivenessStatusResponse(
            ai_system_id=ai_system_id,
            configured=verdict.configured,
            expectation=LivenessExpectationResponse(**expectation.to_dict()),
            state=verdict.state,
            reason=verdict.reason,
            window_hours=verdict.window_hours,
            inbound_calls=verdict.inbound_calls,
            last_activity_at=(
                verdict.last_activity_at.isoformat() if verdict.last_activity_at else None
            ),
            last_run_at=verdict.last_run_at.isoformat() if verdict.last_run_at else None,
            quiet_for_seconds=verdict.quiet_for_seconds,
            has_ever_run=verdict.last_activity_at is not None,
            is_silent=verdict.is_silent,
            last_processed_run_at=(
                verdict.last_processed_run_at.isoformat()
                if verdict.last_processed_run_at
                else None
            ),
            processed_quiet_for_seconds=verdict.processed_quiet_for_seconds,
            work_quiet=verdict.work_quiet,
            work_quiet_reason=verdict.work_quiet_reason,
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @router.put(
        "/{ai_system_id}/liveness",
        response_model=LivenessExpectationResponse,
        summary="Set the liveness expectation",
    )
    def put_liveness(
        ai_system_id: str,
        payload: LivenessExpectationUpdate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> LivenessExpectationResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _require_ai_system(db, resolved, ai_system_id)

        if payload.mode is not None and payload.mode not in LIVENESS_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"mode must be one of {sorted(LIVENESS_MODES)}",
            )

        row = (
            db.query(AISystemLivenessExpectation)
            .filter(
                AISystemLivenessExpectation.tenant_id == resolved,
                AISystemLivenessExpectation.ai_system_id == ai_system_id,
            )
            .first()
        )
        if row is None:
            row = AISystemLivenessExpectation(
                id=f"live-{ULID()}",
                tenant_id=resolved,
                ai_system_id=ai_system_id,
                mode=default_mode_for(system.system_kind),
                created_by=getattr(admin, "username", None),
            )
            db.add(row)

        for field in (
            "mode",
            "expected_min_runs",
            "window_seconds",
            "grace_seconds",
            "enabled",
            "max_quiet_processed_seconds",
        ):
            if field in payload.model_fields_set:
                setattr(row, field, getattr(payload, field))

        # Typing values by hand means this is no longer the learned
        # proposal, and the UI should stop claiming it is.
        row.accepted_from_learned = False

        db.flush()
        db.commit()
        db.refresh(row)

        logger.info(
            "Liveness expectation updated",
            context={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "mode": row.mode,
            },
        )
        return LivenessExpectationResponse(**row.to_dict())

    # ------------------------------------------------------------------
    # Learn / accept
    # ------------------------------------------------------------------

    @router.post(
        "/{ai_system_id}/liveness/learn",
        response_model=CadenceProposalResponse,
        summary="Propose an expectation from observed cadence",
        description=(
            "Derives a window from the 95th-percentile gap between runs — "
            "not the median, which would alert on half of all normal quiet "
            "periods. Writes nothing; use /accept to take it."
        ),
    )
    def learn_liveness(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        lookback_days: int = Query(30, ge=1, le=365),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> CadenceProposalResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _require_ai_system(db, resolved, ai_system_id)

        proposal = liveness_learning_service.propose(
            db, resolved, ai_system_id, lookback_days=lookback_days
        )
        return CadenceProposalResponse(**proposal.to_dict())

    @router.post(
        "/{ai_system_id}/liveness/accept",
        response_model=LivenessExpectationResponse,
        summary="Accept the learned cadence",
    )
    def accept_learned(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        lookback_days: int = Query(30, ge=1, le=365),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> LivenessExpectationResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _require_ai_system(db, resolved, ai_system_id)

        proposal = liveness_learning_service.propose(
            db, resolved, ai_system_id, lookback_days=lookback_days
        )
        if not proposal.confident:
            # Refusing here is the point. An expectation invented from
            # three runs will fire, the operator will stop trusting the
            # category, and the feature dies of false alarms.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Not enough history to learn a cadence yet. "
                    f"{proposal.rationale} Set the expectation manually if you "
                    "already know it."
                ),
            )

        row = (
            db.query(AISystemLivenessExpectation)
            .filter(
                AISystemLivenessExpectation.tenant_id == resolved,
                AISystemLivenessExpectation.ai_system_id == ai_system_id,
            )
            .first()
        )
        if row is None:
            row = AISystemLivenessExpectation(
                id=f"live-{ULID()}",
                tenant_id=resolved,
                ai_system_id=ai_system_id,
                created_by=getattr(admin, "username", None),
            )
            db.add(row)

        row.mode = proposal.mode
        row.window_seconds = proposal.window_seconds
        row.grace_seconds = proposal.grace_seconds
        row.expected_min_runs = proposal.expected_min_runs
        row.enabled = True
        row.learned_from_runs = proposal.learned_from_runs
        row.learned_median_gap_seconds = proposal.median_gap_seconds
        row.learned_p95_gap_seconds = proposal.p95_gap_seconds
        row.learned_at = datetime.now(timezone.utc)
        row.accepted_from_learned = True

        db.flush()
        db.commit()
        db.refresh(row)

        logger.info(
            "Learned liveness cadence accepted",
            context={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "window_seconds": row.window_seconds,
                "learned_from_runs": row.learned_from_runs,
            },
        )
        return LivenessExpectationResponse(**row.to_dict())

    return router
