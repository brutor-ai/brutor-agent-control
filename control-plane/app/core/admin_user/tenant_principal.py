# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The tenant administrator as a principal.

A tenant administrator signs in to one tenant and acts on that tenant only.
It is the principal of the Community Edition: registered as `tenant` in
`app/composition/principals.py`, the value its tokens carry in `admin_type`.

The functions here are what the registry holds for it: how its token is
verified, which tenant a request of its acts on, where its session is, which
administrator rows are its own, where its audit rows go and how its sign-in
is completed after a second factor.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database.models.admin import TenantAdmin, TenantAdminAuditLog, TenantAdminSession
from app.services import auth_read_model
from app.services.session_activity import touch_session_activity

NAME = "tenant"


def verify(service, payload: Dict[str, Any], db: Session) -> Optional[Tuple[TenantAdmin, Optional[TenantAdminSession]]]:
    """The administrator and session a tenant-admin token stands for, or None.

    The read model answers "still active" for a few seconds after the last
    real check; the admin row is always loaded because handlers use it, and
    always by id *and* tenant: a token cannot name an administrator of
    another tenant.
    """
    jwt_id = payload.get('jti')
    user_id = payload.get('user_id')
    tenant_id = payload.get('tenant_id')

    if not tenant_id:
        service.logger.debug("Tenant ID missing from tenant admin token")
        return None

    session = None
    if not service._session_recently_verified(jwt_id):
        session = db.query(TenantAdminSession).filter(
            TenantAdminSession.id == jwt_id,
            TenantAdminSession.is_active == True
        ).first()

        if not session:
            service.logger.debug("Tenant admin session not found", context={"jwt_id": jwt_id})
            return None

        if session.is_expired():
            # Committed, not flushed: the request is refused next (401) and
            # rolled back, and the row would stay active for good.
            # Nothing else is pending: this is the request's first statement.
            session.deactivate("expired")
            db.commit()
            service.logger.debug("Tenant admin session expired", context={"jwt_id": jwt_id})
            return None
        auth_read_model.remember_session(session.id, session.expires_at)

    admin = db.query(TenantAdmin).filter(
        TenantAdmin.id == user_id,
        TenantAdmin.tenant_id == tenant_id
    ).first()

    # Deactivated: refused. LOCKED is deliberately not looked at here: five
    # wrong guesses from anyone who can reach the sign-in form lock an
    # account, and that must not throw a signed-in administrator out. The
    # lock stops new sign-ins (password and second step); an open session
    # goes on answering.
    if not admin or not admin.is_active:
        service.logger.debug("Tenant admin not found or inactive", context={
            "user_id": user_id,
            "tenant_id": tenant_id
        })
        return None

    if session is not None:
        # Own short transaction: the request never locks the row.
        touch_session_activity(db, session)

    return admin, session


def resolve_tenant(admin, tenant_id_query: Optional[str]) -> str:
    """A tenant administrator is pinned to its own tenant.

    Naming another tenant is refused, not ignored. Ignoring the parameter
    answers a question nobody asked: the operator requested tenant B and gets
    tenant A's rows back, rendered under a heading that says B. On a write
    path — erasure, a config change — they believe they acted on B and
    actually acted on A. There is no reading under which silently
    substituting a different tenant is the safer failure.
    """
    own = getattr(admin, "tenant_id", None)
    if not own:
        # An administrator of no tenant is not a tenant administrator.
        raise HTTPException(status_code=403, detail="Administrator has no tenant")
    if tenant_id_query and tenant_id_query != own:
        raise HTTPException(
            status_code=403,
            detail="Tenant admins can only act on their own tenant",
        )
    return own


def find_session(db: Session, session_id: str) -> Optional[TenantAdminSession]:
    return db.query(TenantAdminSession).filter(TenantAdminSession.id == session_id).first()


def owns(admin: Any) -> bool:
    """Is `admin` a tenant administrator's row?"""
    return isinstance(admin, TenantAdmin)


def find_admin(db: Session, admin_id: Any) -> Optional[TenantAdmin]:
    return db.query(TenantAdmin).filter(TenantAdmin.id == admin_id).first()


def active_sessions(db: Session, admin_id: Any) -> List[TenantAdminSession]:
    """The live sessions of a tenant administrator."""
    return (
        db.query(TenantAdminSession)
        .filter(TenantAdminSession.admin_id == admin_id, TenantAdminSession.is_active.is_(True))
        .all()
    )


def audit_entry(
    db: Session,
    logger,
    *,
    admin: Any,
    admin_id: Optional[str],
    tenant_id: Optional[str],
    affected_tenant_id: Optional[str],
    **fields: Any,
) -> Optional[TenantAdminAuditLog]:
    """The row of a tenant administrator's action, in its tenant's trail; or
    None when there is no tenant to file it under (a warning is logged).

    An administrator's row is its own tenant's, whatever tenant the caller
    names. `affected_tenant_id` is the system trail's column and is not used.
    """
    if admin is not None:
        admin_id = admin.id
        tenant_id = admin.tenant_id

    if not tenant_id:
        logger.warning(
            "Tenant admin audit log without tenant_id",
            context={
                "action": fields.get("action"),
                "admin_id": admin_id
            }
        )
        return None

    # Failed-login audit rows are the most common case where the tenant is
    # operator-supplied (from a login form) and may not correspond to a real
    # tenant — e.g. someone typing "default2" when no such tenant exists.
    # Inserting would trip the FK constraint, which is then caught by the
    # logger but emits a noisy ~80-line SQLAlchemy stack trace. Pre-check
    # existence so the audit attempt for an unknown tenant downgrades to a
    # single-line WARNING.
    from app.database.models.tenant import Tenant
    tenant_exists = (
        db.query(Tenant.id)
        .filter(Tenant.id == tenant_id)
        .first()
    )
    if not tenant_exists:
        logger.warning(
            "Audit log skipped — tenant does not exist",
            context={
                "action": fields.get("action"),
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "success": fields.get("success"),
            }
        )
        return None

    return TenantAdminAuditLog(tenant_id=tenant_id, admin_id=admin_id, **fields)


def complete_sign_in(
    service,
    admin: TenantAdmin,
    challenge: Dict[str, Any],
    db: Session,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """The answer of `POST /login/2fa` to a tenant administrator whose code
    was right: a session of the tenant the challenge names.

    `create_admin_jwt` also persists the matching TenantAdminSession row so
    subsequent requests pass the session lookup in verify_token (the JWT
    alone is not enough — the jti must resolve to an active session).
    """
    tenant_id = challenge.get("tenant_id")
    token = service.create_admin_jwt(
        admin,
        tenant_id,
        db=db,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    from app.database.models.tenant import Tenant
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()

    return {
        "token": token,
        "admin": admin.to_dict(),
        "tenant_id": tenant_id,
        "tenant_display_name": tenant.display_name if tenant else None,
        "is_trial": tenant.is_trial if tenant else False,
        "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant and tenant.trial_ends_at else None,
        "suspended": tenant.suspended if tenant else False,
    }
