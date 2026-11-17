# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Semantic Governance Policy admin API — `/v1/admin/semantic-policies/*`.

CRUD over `semantic_policies` + resource-group binding. The Rust runtime loads
these and evaluates each governed request against the NL constraint via an LLM
judge (shadow → enforce). Read → SEMANTIC_POLICY_READ; write → _WRITE.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.sync_bridge import block_on
from app.api.v1.models.admin.semantic_policy import (
    SemanticPolicyCreateRequest,
    SemanticPolicyListResponse,
    SemanticPolicyResponse,
    SemanticPolicyUpdateRequest,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.cache_invalidation import invalidate_proxy_cache
from app.database.models.resource_group import ResourceGroup
from app.database.models.semantic_policy import SemanticPolicy


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


# Nullable columns, where null is a real state: no description; every surface;
# the tenant's default judge model.
_CLEARABLE_FIELDS = frozenset({"description", "surfaces", "judge_model_id"})
_PATCHABLE_FIELDS = _CLEARABLE_FIELDS | {"name", "constraint_text", "action", "mode", "enabled"}


def create_semantic_policies_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/semantic-policies", tags=["Semantic Policies"])

    def _policy_or_404(db: Session, tid: str, pid: str) -> SemanticPolicy:
        p = (
            db.query(SemanticPolicy)
            .filter(SemanticPolicy.id == pid, SemanticPolicy.tenant_id == tid)
            .first()
        )
        if p is None:
            raise HTTPException(status_code=404, detail="Semantic policy not found")
        return p

    def _sync_system_bindings(db: Session, tid: str) -> None:
        """Keep the judge model bound to the policy's resource groups.

        The judge call is governed like any other request, so the calling
        group must actually have access to the judge model — otherwise the
        policy fails open at runtime. Rather than exempt the judge from
        access control, we make the binding follow the configuration.

        Never fatal: a sync problem must not block the admin's save. The
        policy is still correct, and the next save (or a manual binding)
        reconciles it.
        """
        from app.services import system_model_bindings

        try:
            # In a savepoint: on Postgres a failed statement aborts the whole
            # transaction, which would lose the save this promises not to block.
            with db.begin_nested():
                stats = system_model_bindings.sync_tenant(db, tid)
            if stats["added"] or stats["removed"]:
                logger.info("Synced system model bindings", context={
                    "tenant_id": tid, "source": "semantic_policy", **stats,
                })
        except Exception as e:  # noqa: BLE001 — best-effort by design
            logger.warning("System model binding sync failed", context={
                "tenant_id": tid, "source": "semantic_policy", "error": str(e),
            })

    def _bind_groups(db: Session, tid: str, policy: SemanticPolicy, group_ids):
        if group_ids is None:
            return
        groups = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.id.in_(group_ids), ResourceGroup.tenant_id == tid)
            .all()
            if group_ids
            else []
        )
        # A policy with no groups is the tenant default, so dropping an unknown
        # id silently could turn a group-scoped policy into a tenant-wide one.
        missing = sorted(set(group_ids) - {g.id for g in groups})
        if missing:
            raise ValueError(f"Resource groups not found in this tenant: {missing}")
        policy.groups = groups

    @router.get("", response_model=SemanticPolicyListResponse, summary="List semantic policies")
    def list_policies(
        tenant_id: Optional[str] = Query(None),
        mode: Optional[str] = Query(None),
        enabled: Optional[bool] = Query(None),
        q: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.SEMANTIC_POLICY_READ)),
    ) -> SemanticPolicyListResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        query = db.query(SemanticPolicy).filter(SemanticPolicy.tenant_id == tid)
        if mode:
            query = query.filter(SemanticPolicy.mode == mode)
        if enabled is not None:
            query = query.filter(SemanticPolicy.enabled == enabled)
        if q:
            query = query.filter(SemanticPolicy.name.ilike(f"%{q}%"))
        rows = query.order_by(SemanticPolicy.created_at.desc()).all()
        return SemanticPolicyListResponse(
            policies=[SemanticPolicyResponse(**r.to_dict()) for r in rows], total=len(rows),
        )

    @router.post("", response_model=SemanticPolicyResponse, status_code=201,
                 summary="Create a semantic policy")
    def create_policy(
        body: SemanticPolicyCreateRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.SEMANTIC_POLICY_WRITE)),
    ) -> SemanticPolicyResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        if db.query(SemanticPolicy).filter(
            SemanticPolicy.tenant_id == tid, SemanticPolicy.name == body.name
        ).first():
            raise HTTPException(status_code=409, detail=f"Policy {body.name!r} already exists")
        try:
            row = SemanticPolicy(
                id=f"sgp-{ULID()}",
                tenant_id=tid,
                name=body.name,
                description=body.description,
                constraint_text=body.constraint_text,
                action=body.action,
                mode=body.mode,
                surfaces=body.surfaces,
                judge_model_id=body.judge_model_id,
                enabled=True,
                created_by=getattr(admin, "username", None) or getattr(admin, "id", None),
            )
            _bind_groups(db, tid, row, body.resource_group_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.add(row)
        # Flush so the group bindings exist before the sync reads them.
        db.flush()
        _sync_system_bindings(db, tid)
        db.commit()
        # Semantic policies are an enforcement control (fail_open=false
        # by default), and the proxy holds them in a TTL cache keyed on
        # tenant+group. Without this publish, a policy created here is
        # not enforced until that TTL lapses — traffic the operator
        # believes is governed goes through ungoverned. Same publish
        # shape as argument_policies.py / guardrails.py.
        block_on(invalidate_proxy_cache(
            request, "semantic_policy", tid, row.id, "create"
        ))
        return SemanticPolicyResponse(**row.to_dict())

    @router.get("/{policy_id}", response_model=SemanticPolicyResponse,
                summary="Semantic policy detail")
    def get_policy(
        policy_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.SEMANTIC_POLICY_READ)),
    ) -> SemanticPolicyResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        return SemanticPolicyResponse(**_policy_or_404(db, tid, policy_id).to_dict())

    @router.patch("/{policy_id}", response_model=SemanticPolicyResponse,
                  summary="Update a semantic policy")
    def update_policy(
        policy_id: str,
        body: SemanticPolicyUpdateRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.SEMANTIC_POLICY_WRITE)),
    ) -> SemanticPolicyResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = _policy_or_404(db, tid, policy_id)
        try:
            # `model_fields_set` tells an explicit null from an omitted key:
            # null clears a nullable column, and is dropped for a required one
            # (there is no state to move to — same rule as `version`).
            for field in _PATCHABLE_FIELDS & body.model_fields_set:
                val = getattr(body, field)
                if val is not None or field in _CLEARABLE_FIELDS:
                    setattr(row, field, val)
            _bind_groups(db, tid, row, body.resource_group_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # A judge-model or access-control change reshapes what must be
        # bound; the sync adds the new pairs and reclaims the stale ones.
        db.flush()
        _sync_system_bindings(db, tid)
        db.commit()
        # The disable direction is the one that bites: a mis-firing
        # policy toggled `enabled=false` keeps refusing calls for the
        # whole TTL window unless the proxy is told to drop it.
        block_on(invalidate_proxy_cache(
            request, "semantic_policy", tid, row.id, "update"
        ))
        return SemanticPolicyResponse(**row.to_dict())

    @router.delete("/{policy_id}", status_code=204, summary="Delete a semantic policy")
    def delete_policy(
        policy_id: str,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.SEMANTIC_POLICY_WRITE)),
    ):
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        db.delete(_policy_or_404(db, tid, policy_id))
        # Flush the delete so the sync no longer sees this policy's
        # requirement and can reclaim the binding it created.
        db.flush()
        _sync_system_bindings(db, tid)
        db.commit()
        block_on(invalidate_proxy_cache(
            request, "semantic_policy", tid, policy_id, "delete"
        ))

    return router
