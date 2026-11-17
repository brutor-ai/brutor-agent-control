# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Identity admin API — `/v1/admin/agent-identities/*`.

Manage agent principals, their per-action grants (consumed by the Rust runtime
authz engine), and their resource-group memberships. Brutor decides/enforces/
proves against agent identity; issuance stays with the IdP.

Read → AGENT_IDENTITY_READ; write → AGENT_IDENTITY_WRITE.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.admin.agent_identity import (
    AgentGrantCreateRequest,
    AgentGrantListResponse,
    AgentGrantResponse,
    AgentIdentityCreateRequest,
    AgentIdentityListResponse,
    AgentIdentityResponse,
    AgentIdentityUpdateRequest,
    AgentMembershipCreateRequest,
    AgentMembershipListResponse,
    AgentMembershipResponse,
    AgentReleaseListResponse,
    AgentReleaseResponse,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.agent_identity import (
    AgentGrant,
    AgentIdentity,
    ResourceGroupAgentMember,
)
from app.services import agent_implementation as impl


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {value!r}")


def _apply_implementation(db: Session, a: AgentIdentity, data: dict, by: Optional[str]) -> None:
    """Validate and set the implementation declaration.
    Only the keys present in `data` change. 422 on an invalid pattern.

    `approved_versions` goes through the one writer that also keeps the
    approval history: a run is judged by
    the declaration in effect when it ran, so every change needs its
    moment. `a` must already be in the session."""
    try:
        if "implementation_name" in data:
            a.implementation_name = impl.normalise_implementation_name(data["implementation_name"])
        if "approved_versions" in data:
            impl.record_approved_versions(db, a, data["approved_versions"], by=by)
        if "require_release" in data and data["require_release"] is not None:
            a.require_release = bool(data["require_release"])
    except impl.DeclarationError as e:
        raise HTTPException(status_code=422, detail=str(e))


def create_agent_identities_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/agent-identities", tags=["Agent Identities"])

    def _agent_or_404(db: Session, tid: str, agent_id: str) -> AgentIdentity:
        a = (
            db.query(AgentIdentity)
            .filter(AgentIdentity.id == agent_id, AgentIdentity.tenant_id == tid)
            .first()
        )
        if a is None:
            raise HTTPException(status_code=404, detail="Agent identity not found")
        return a

    # ── Identities ──
    @router.get("", response_model=AgentIdentityListResponse, summary="List agent identities")
    def list_agents(
        tenant_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        q: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_READ)),
    ) -> AgentIdentityListResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        query = db.query(AgentIdentity).filter(AgentIdentity.tenant_id == tid)
        if status:
            query = query.filter(AgentIdentity.status == status)
        if q:
            query = query.filter(AgentIdentity.name.ilike(f"%{q}%"))
        rows = query.order_by(AgentIdentity.created_at.desc()).all()
        return AgentIdentityListResponse(
            agents=[AgentIdentityResponse(**r.to_dict()) for r in rows], total=len(rows),
        )

    @router.post("", response_model=AgentIdentityResponse, status_code=201, summary="Create an agent identity")
    def create_agent(
        body: AgentIdentityCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ) -> AgentIdentityResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        # Feature + quota gates: agents surface + Max Agent Identities.
        TenantService.check_feature_access(db, tid, 'a2a')
        TenantService.check_quota(db, tid, TenantResourceTypes.AGENT_IDENTITIES)
        if db.query(AgentIdentity).filter(
            AgentIdentity.tenant_id == tid, AgentIdentity.name == body.name
        ).first():
            raise HTTPException(status_code=409, detail=f"Agent {body.name!r} already exists")
        try:
            row = AgentIdentity(
                id=f"agent-{ULID()}",
                tenant_id=tid,
                name=body.name,
                description=body.description,
                owner_user_id=body.owner_user_id,
                owner_label=body.owner_label,
                idp_server_id=body.idp_server_id,
                idp_subject=body.idp_subject,
                source=body.source,
                enabled=True,
                status="active",
                default_deny=body.default_deny,
                enforcement_mode=body.enforcement_mode,
                expires_at=_parse_dt(body.expires_at),
                created_by=getattr(admin, "username", None) or getattr(admin, "id", None),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.add(row)
        _apply_implementation(db, row, body.model_dump(
            include={"implementation_name", "approved_versions", "require_release"}),
            by=getattr(admin, "username", None) or getattr(admin, "id", None))
        db.commit()
        return AgentIdentityResponse(**row.to_dict())

    @router.get("/{agent_id}", response_model=AgentIdentityResponse, summary="Agent identity detail")
    def get_agent(
        agent_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_READ)),
    ) -> AgentIdentityResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        return AgentIdentityResponse(**_agent_or_404(db, tid, agent_id).to_dict())

    @router.patch("/{agent_id}", response_model=AgentIdentityResponse, summary="Update an agent identity")
    def update_agent(
        agent_id: str,
        body: AgentIdentityUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ) -> AgentIdentityResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        a = _agent_or_404(db, tid, agent_id)
        data = body.model_dump(exclude_unset=True)
        try:
            for field in ("name", "description", "owner_user_id", "owner_label",
                          "idp_server_id", "idp_subject", "enabled", "status", "default_deny",
                          "enforcement_mode"):
                if field in data:
                    setattr(a, field, data[field])
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if "expires_at" in data:
            a.expires_at = _parse_dt(data["expires_at"])
        # null approved_versions in a PATCH means "not sent", like every
        # other optional field here; clearing is an explicit [].
        if data.get("approved_versions", ()) is None:
            data.pop("approved_versions")
        _apply_implementation(db, a, data,
                              by=getattr(admin, "username", None) or getattr(admin, "id", None))
        db.commit()
        return AgentIdentityResponse(**a.to_dict())

    @router.post("/{agent_id}/revoke", response_model=AgentIdentityResponse, summary="Revoke an agent identity")
    def revoke_agent(
        agent_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ) -> AgentIdentityResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        a = _agent_or_404(db, tid, agent_id)
        a.enabled = False
        a.status = "suspended"
        a.revoked_at = datetime.now(timezone.utc)
        db.commit()
        return AgentIdentityResponse(**a.to_dict())

    # ── Releases ──
    @router.get(
        "/{agent_id}/releases",
        response_model=AgentReleaseListResponse,
        summary="Observed releases of an agent identity's software",
        description=(
            "One entry per (name, version, build) the gateway observed for this "
            "identity, most recently seen first. Written by the core's run "
            "finaliser. `approved` is derived at read time from the identity's "
            "approved_versions (null = observe only); `trust` says whether the "
            "release came from a verified token claim or was self-declared."
        ),
    )
    def list_releases(
        agent_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_READ)),
    ) -> AgentReleaseListResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        a = _agent_or_404(db, tid, agent_id)
        rows = impl.releases_of(db, tid, agent_id)
        current_id = rows[0].id if rows else None
        approved = list(a.approved_versions or [])
        return AgentReleaseListResponse(
            agent_id=agent_id,
            implementation_name=a.implementation_name,
            approved_versions=approved,
            require_release=bool(a.require_release),
            current_release_id=current_id,
            releases=[
                AgentReleaseResponse(
                    **impl.release_view(r, approved, a.implementation_name),
                    current=r.id == current_id,
                )
                for r in rows
            ],
            total=len(rows),
        )

    # ── Grants ──
    @router.get("/{agent_id}/grants", response_model=AgentGrantListResponse, summary="List agent grants")
    def list_grants(
        agent_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_READ)),
    ) -> AgentGrantListResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        _agent_or_404(db, tid, agent_id)
        rows = db.query(AgentGrant).filter(
            AgentGrant.tenant_id == tid, AgentGrant.agent_id == agent_id
        ).all()
        return AgentGrantListResponse(
            grants=[AgentGrantResponse(**r.to_dict()) for r in rows], total=len(rows),
        )

    @router.post("/{agent_id}/grants", response_model=AgentGrantResponse, status_code=201, summary="Add an agent grant")
    def add_grant(
        agent_id: str,
        body: AgentGrantCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ) -> AgentGrantResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        _agent_or_404(db, tid, agent_id)
        try:
            row = AgentGrant(
                id=f"grant-{ULID()}",
                tenant_id=tid,
                agent_id=agent_id,
                action_type=body.action_type,
                target=body.target,
                effect=body.effect,
                constraints=body.constraints,
                enabled=True,
                expires_at=_parse_dt(body.expires_at),
                created_by=getattr(admin, "username", None) or getattr(admin, "id", None),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.add(row)
        db.commit()
        return AgentGrantResponse(**row.to_dict())

    @router.delete("/{agent_id}/grants/{grant_id}", status_code=204, summary="Delete an agent grant")
    def delete_grant(
        agent_id: str,
        grant_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ):
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = db.query(AgentGrant).filter(
            AgentGrant.id == grant_id, AgentGrant.agent_id == agent_id,
            AgentGrant.tenant_id == tid,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Grant not found")
        db.delete(row)
        db.commit()

    # ── Memberships ──
    @router.get("/{agent_id}/memberships", response_model=AgentMembershipListResponse, summary="List agent RG memberships")
    def list_memberships(
        agent_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_READ)),
    ) -> AgentMembershipListResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        _agent_or_404(db, tid, agent_id)
        rows = db.query(ResourceGroupAgentMember).filter(
            ResourceGroupAgentMember.tenant_id == tid,
            ResourceGroupAgentMember.agent_id == agent_id,
        ).all()
        return AgentMembershipListResponse(
            memberships=[AgentMembershipResponse(**r.to_dict()) for r in rows], total=len(rows),
        )

    @router.post("/{agent_id}/memberships", response_model=AgentMembershipResponse, status_code=201, summary="Add an agent to a resource group")
    def add_membership(
        agent_id: str,
        body: AgentMembershipCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ) -> AgentMembershipResponse:
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        _agent_or_404(db, tid, agent_id)
        existing = db.query(ResourceGroupAgentMember).filter(
            ResourceGroupAgentMember.tenant_id == tid,
            ResourceGroupAgentMember.agent_id == agent_id,
            ResourceGroupAgentMember.group_id == body.group_id,
        ).first()
        if existing:
            return AgentMembershipResponse(**existing.to_dict())
        row = ResourceGroupAgentMember(
            tenant_id=tid, agent_id=agent_id, group_id=body.group_id, role=body.role,
            added_by=getattr(admin, "username", None) or getattr(admin, "id", None),
        )
        db.add(row)
        db.commit()
        return AgentMembershipResponse(**row.to_dict())

    @router.delete("/{agent_id}/memberships/{member_id}", status_code=204, summary="Remove an agent from a resource group")
    def remove_membership(
        agent_id: str,
        member_id: int,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AGENT_IDENTITY_WRITE)),
    ):
        admin, admin_type = admin_and_type
        tid = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = db.query(ResourceGroupAgentMember).filter(
            ResourceGroupAgentMember.id == member_id,
            ResourceGroupAgentMember.agent_id == agent_id,
            ResourceGroupAgentMember.tenant_id == tid,
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Membership not found")
        db.delete(row)
        db.commit()

    return router
