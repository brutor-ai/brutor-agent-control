# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin Authentication Endpoints for Multi-Tenant RBAC
Separate endpoints for SystemAdmin and TenantAdmin login
"""

from uuid import uuid4
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Literal
from fastapi import APIRouter, HTTPException, Depends, Request, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_ as sa_or

from app.core.sync_bridge import block_on
from app.constants.admin import AdminType
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.services.admin_export import (
    AUDIT_LOG_CSV_HEADERS,
    assert_within_cap,
    build_meta,
    export_filename,
    flatten_audit_log,
    media_type_for,
    stream_csv,
    stream_jsonl,
)
from app.database import get_db
from app.composition.permissions import PERMISSIONS
from app.core.admin_user.permissions import Permission
from app.composition.principals import PRINCIPALS
from app.database.models.admin import (
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.admin_user import lockout
from app.core.admin_user.services.session_revocation import deactivate_admin_sessions
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.admin_user.dependencies import (
    get_auth_service,
    get_audit_logger,
    get_current_admin, is_installation_operator, is_platform_wide, may_act_on_tenant, require_permission,
    resolve_tenant_id, tenant_scope,
)
from app.api.v1.models.admin.admin_rbac import (
    LoginResponse, SystemAdminLoginRequest, AdminResponse, CurrentAdminResponse,
    TenantAdminLoginRequest, CreateTenantAdminRequest, LogoutResponse,
    RoleListResponse, RoleResponse, CreateRoleRequest, UpdateRoleRequest,
    AssignRoleRequest, PermissionsListResponse, PermissionInfo, UpdateTenantAdminRequest
)
from app.api.v1.models.admin.in_use import IN_USE_RESPONSE, in_use
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def refuse_role_beyond_the_caller(admin, role) -> None:
    """403 when `role` holds `system:admin` and the caller does not.

    `system:admin` answers yes to every permission check. Writing it into a
    role already takes holding it; handing out a role that holds it — to
    another administrator, or to a new one — takes the same. Without this a
    tenant administrator with `roles:assign` or `admin:user:create` could give a
    second account a role the system administrator had made in the tenant.
    """
    if role is None:
        return
    if Permission.SYSTEM_ADMIN.value in role.get_permissions() and not admin.has_permission(
        Permission.SYSTEM_ADMIN.value
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only system administrators can grant system:admin permission"
        )


def create_admin_users_router(logger: BrutorLogger) -> APIRouter:
    """Create the authentication router"""
    router = APIRouter(prefix="/admin/users", tags=["Admin Authentication"])

    # The system administrator's login (`POST /admin/users/system/login`) is
    # the Enterprise router `system_admin_login.py`.

    # =========================================================================
    # TENANT ADMIN AUTHENTICATION
    # =========================================================================

    @router.post(
        "/tenant/login",
        summary="Tenant Admin Login",
        description="""
        Login endpoint for tenant administrators.

        Returns either a full login response on success, or a 2FA
        challenge payload (`{requires_2fa: true, temp_token, ...}`) when
        the admin has TOTP enabled — the client must then call
        `POST /v1/admin/users/login/2fa` with the temp_token and code.

        When the tenant's two-factor policy requires 2FA of administrators
        and the admin has none, no session is issued either: the answer is
        `{requires_2fa_enrollment: true, temp_token, admin_type, tenant_id,
        tenant_display_name, admin}` and the client enrols through
        `POST /v1/admin/users/login/2fa/enroll/setup` and `.../enroll/verify`.
        """
    )
    # sync `def` — runs in the threadpool. Same rationale as
    # system_admin_login: blocking bcrypt + sync SQLAlchemy must not run on the
    # event loop. No `await` in the body.
    def tenant_admin_login(
        request: Request,
        login_request: TenantAdminLoginRequest,
        db: Session = Depends(get_db),
        auth_service: UnifiedAuthService = Depends(get_auth_service),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Authenticate tenant administrator"""

        ip_address = client_ip(request)
        user_agent = request.headers.get('user-agent')

        result = auth_service.login_tenant_admin(
            username=login_request.username,
            password=login_request.password,
            tenant_id=login_request.tenant_id,
            db=db,
            ip_address=ip_address,
            user_agent=user_agent
        )

        if not result:

            audit.log(
                action="tenant_admin_login_failed",
                admin=None,
                admin_type=AdminType.TENANT_ADMIN_TYPE,
                tenant_id=login_request.tenant_id,
                success=False,
                resource_type="auth",
                resource_id=login_request.username,
                details={
                    "username": login_request.username,
                    "tenant_id": login_request.tenant_id
                },
                ip_address=ip_address,
                user_agent=user_agent,
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
            )

        # 2FA challenge: password verified, but the admin has TOTP
        # enabled. Return the challenge dict verbatim — the client will
        # call /login/2fa next with temp_token and code. No real JWT is
        # issued until the second factor verifies.
        if result.get('requires_2fa'):
            audit.log(
                action="tenant_admin_login_2fa_challenge",
                admin=None,
                admin_type=AdminType.TENANT_ADMIN_TYPE,
                tenant_id=result.get('tenant_id'),
                success=True,
                resource_type="auth",
                resource_id=login_request.username,
                details={
                    "username": login_request.username,
                    "tenant_id": result.get('tenant_id'),
                },
                ip_address=ip_address,
                user_agent=user_agent,
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )
            return result

        # Enrolment owed: the password verified, the tenant's policy
        # requires 2FA of administrators and this one has none. Like the
        # challenge above, no session was issued; the client enrols through
        # /login/2fa/enroll/setup and /verify with the temp_token.
        if result.get('requires_2fa_enrollment'):
            audit.log(
                action="tenant_admin_login_2fa_enrollment_required",
                admin=None,
                admin_type=AdminType.TENANT_ADMIN_TYPE,
                tenant_id=result.get('tenant_id'),
                success=True,
                resource_type="auth",
                resource_id=login_request.username,
                details={
                    "username": login_request.username,
                    "tenant_id": result.get('tenant_id'),
                },
                ip_address=ip_address,
                user_agent=user_agent,
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )
            return result

        # Get the admin object from result
        admin = db.query(TenantAdmin).filter(TenantAdmin.id == result['admin']['id']).first()

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = admin.tenant_id

        audit.log(
            action="tenant_admin_login",
            admin=admin,
            admin_type=AdminType.TENANT_ADMIN_TYPE,
            tenant_id=admin_tenant_id,
            success=True,
            resource_type="auth",
            resource_id=admin_id,
            details={
                "username": login_request.username,
                "tenant_id": login_request.tenant_id,
                "roles": result['admin'].get('roles', [])
            },
            ip_address=ip_address,
            user_agent=user_agent,
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        # Duplicate of the log inside login_tenant_admin; keep at DEBUG
        # for the same reason — successful logins are audit-log
        # material, not operational-log material.
        logger.debug("Tenant admin logged in", context={
            "admin_id": result['admin']['id'],
            "username": login_request.username,
            "tenant_id": login_request.tenant_id
        })

        # Return the raw dict so all fields (tenant_id, tenant_display_name,
        # is_trial, trial_ends_at, suspended, suspension_reason) reach the
        # client. Constructing LoginResponse(**result) was silently dropping
        # the extra fields under Pydantic V2's default `extra='ignore'`.
        return result

    @router.post(
        "/tenant",
        response_model=AdminResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create Tenant Admin",
        description="""
        Create a new tenant administrator.
        
        - System admins: Can create admins for any tenant
        - Tenant admins: Can only create admins for their own tenant (if they have permission)
        """
    )
    def create_tenant_admin(
        request: Request,
        create_request: CreateTenantAdminRequest,
        db: Session = Depends(get_db),
        current_admin_and_type = Depends(get_current_admin),
        auth_service: UnifiedAuthService = Depends(get_auth_service),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ADMIN_USER_CREATE))
    ):
        """Create new tenant administrator"""

        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        # Tenant admins can only create admins for their own tenant
        # (asked of the principal: a type nobody registered creates none)
        if not may_act_on_tenant(current_admin, admin_type, create_request.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant admins can only create admins for their own tenant"
            )

        # A role that holds `system:admin` is handed out only by a caller
        # that holds it. A role of another tenant, or none, is the
        # service's to refuse below.
        if create_request.role_id:
            refuse_role_beyond_the_caller(current_admin, db.query(TenantRole).filter(
                TenantRole.id == create_request.role_id,
                TenantRole.tenant_id == create_request.tenant_id,
            ).first())

        # Check tenant quota
        TenantService.check_quota(db, create_request.tenant_id, TenantResourceTypes.ADMIN_USERS)

        try:
            admin = auth_service.create_tenant_admin(
                username=create_request.username,
                email=create_request.email.__str__(),
                password=create_request.password,
                tenant_id=create_request.tenant_id,
                full_name=create_request.full_name,
                role_id=create_request.role_id,  # Pass role_id from request
                created_by=current_admin_id,
                db=db
            )

            # Eager load newly created admin attributes
            created_admin_id = admin.id

            audit.log(
                action="tenant_admin_created",
                admin=current_admin,
                admin_type=admin_type,
                tenant_id=create_request.tenant_id,
                success=True,
                resource_type="tenant_admin",
                resource_id=created_admin_id,
                details={
                    "username": create_request.username,
                    "tenant_id": create_request.tenant_id,
                    "role_id": admin.role_id,  # Log assigned role_id
                    "role_name": admin.role.name if admin.role else None,  # Log role name
                    "created_admin_id": created_admin_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Tenant admin created", context={
                "admin_id": created_admin_id,
                "username": create_request.username,
                "tenant_id": create_request.tenant_id,
                "created_by": current_admin_id
            })

            return AdminResponse(**admin.to_dict())

        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e)
            )

    @router.get(
        "/tenant",
        response_model=Dict[str, Any],
        summary="List Tenant Admins",
        description="""
        List all tenant administrators.

        - System admins: Can list admins from all tenants (optionally filter by tenant_id)
        - Tenant admins: Can only list admins from their own tenant
        """
    )
    def list_tenant_admins(
            request: Request,
            tenant_id: Optional[str] = None,  # Optional filter for system admins
            include_inactive: bool = False,
            db: Session = Depends(get_db),
            current_admin_and_type=Depends(get_current_admin),
            auth_service: UnifiedAuthService = Depends(get_auth_service),
            _: Any = Depends(require_permission(Permission.ADMIN_USER_READ))
    ) -> Dict[str, Any]:
        """List tenant administrators"""
        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        # Determine which tenant(s) to query
        if admin_type == AdminType.TENANT_ADMIN_TYPE.value:
            # Tenant admins can only see their own tenant
            query_tenant_id = current_admin.tenant_id
        elif is_platform_wide(admin_type):
            # System admins can filter by tenant_id or see all
            query_tenant_id = tenant_id
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unauthorized admin type"
            )

        try:
            # Build query
            query = db.query(TenantAdmin)

            if query_tenant_id:
                query = query.filter(TenantAdmin.tenant_id == query_tenant_id)

            if not include_inactive:
                query = query.filter(TenantAdmin.is_active == True)

            admins = query.all()

            # Convert to dict
            admins_list = [admin.to_dict() for admin in admins]

            logger.debug("Listed tenant admins", context={
                "requester_id": current_admin_id,
                "requester_type": admin_type,
                "tenant_id": query_tenant_id,
                "count": len(admins_list)
            })

            return {
                "admins": admins_list,
                "total": len(admins_list),
                "filters": {
                    "tenant_id": query_tenant_id,
                    "include_inactive": include_inactive
                },
                "accessed_by": {
                    "user_id": current_admin_id,
                    "username": current_admin_username,
                    "user_roles": ", ".join(getattr(current_admin, 'roles', []))
                }
            }

        except Exception as e:
            logger.error("Failed to list tenant admins", context={
                "requester_id": current_admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list tenant admins: {str(e)}"
            )

    # =========================================================================
    # TENANT ADMIN SESSION MANAGEMENT
    #
    # View and revoke active JWT sessions for the tenant's admins. This
    # is the "oh my account was compromised" kill-switch plus day-to-day
    # visibility into where and when admins are signed in. Tenant-scoped
    # for tenant admins; system admins can target any tenant by passing
    # tenant_id=....
    #
    # IMPORTANT: These routes MUST be registered before
    # /tenant/{admin_id} — FastAPI matches in declaration order, so a
    # literal `/tenant/sessions` declared after the path-param route
    # gets swallowed as `admin_id=sessions` and returns 404.
    # =========================================================================

    @router.get(
        "/tenant/sessions",
        summary="List Tenant Admin Sessions",
        description=(
            "List JWT sessions for the caller's tenant admins. Tenant "
            "admins see only their own tenant; system admins may pass "
            "`tenant_id` to target a specific tenant, or omit it to see "
            "every tenant."
        ),
    )
    def list_tenant_admin_sessions(
        request: Request,
        tenant_id: Optional[str] = Query(
            None,
            description="System admins only — scope the listing to one tenant. Ignored for tenant admins.",
        ),
        include_inactive: bool = Query(
            False,
            description="If true, include revoked/expired sessions. Defaults to active-only.",
        ),
        limit: int = Query(200, ge=1, le=1000),
        db: Session = Depends(get_db),
        current_admin_and_type=Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.ADMIN_USER_READ)),
    ):
        current_admin, admin_type = current_admin_and_type

        q = db.query(TenantAdminSession)

        # Tenant admins are locked to their own tenant (naming another is
        # refused); the system administrator sees the tenant it names, or
        # all; a type nobody registered sees none (403).
        scope_tenant_id = tenant_scope(current_admin, admin_type, tenant_id)
        if scope_tenant_id:
            q = q.filter(TenantAdminSession.tenant_id == scope_tenant_id)

        if not include_inactive:
            q = q.filter(TenantAdminSession.is_active == True)  # noqa: E712

        # Newest first — typically you want to inspect the most recent
        # sign-ins, and the current session is almost always there.
        rows = q.order_by(desc(TenantAdminSession.created_at)).limit(limit).all()

        # Eager-load admin usernames/emails in one round-trip.
        admin_ids = {s.admin_id for s in rows}
        admins_by_id: Dict[str, TenantAdmin] = {}
        if admin_ids:
            for a in db.query(TenantAdmin).filter(TenantAdmin.id.in_(admin_ids)).all():
                admins_by_id[a.id] = a

        # Identify the caller's own current session so the UI can mark
        # it "This device" and warn before revoking. We hash the caller's
        # bearer token the same way create_admin_jwt does and match.
        import hashlib as _hashlib
        current_session_id: Optional[str] = None
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header.split(" ", 1)[1].strip()
            if bearer:
                caller_hash = _hashlib.sha256(bearer.encode()).hexdigest()
                for s in rows:
                    if s.token_hash == caller_hash:
                        current_session_id = s.id
                        break

        def _serialize(s: TenantAdminSession) -> Dict[str, Any]:
            a = admins_by_id.get(s.admin_id)
            return {
                "id": s.id,
                "tenant_id": s.tenant_id,
                "admin_id": s.admin_id,
                "admin_username": a.username if a else None,
                "admin_email": a.email if a else None,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent,
                "risk_score": s.risk_score,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_activity": s.last_activity.isoformat() if s.last_activity else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "is_active": s.is_active,
                "is_expired": s.is_expired(),
                "deactivation_reason": s.deactivation_reason,
                "is_current": s.id == current_session_id,
            }

        return {
            "sessions": [_serialize(s) for s in rows],
            "total": len(rows),
            "current_session_id": current_session_id,
        }

    @router.post(
        "/tenant/sessions/{session_id}/revoke",
        summary="Revoke a Tenant Admin Session",
        description=(
            "Deactivate the given session so the associated JWT is "
            "rejected on the next request. Tenant admins may only "
            "revoke sessions in their own tenant."
        ),
    )
    def revoke_tenant_admin_session(
        session_id: str,
        request: Request,
        db: Session = Depends(get_db),
        current_admin_and_type=Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ADMIN_USER_UPDATE)),
    ):
        current_admin, admin_type = current_admin_and_type

        session_row = db.query(TenantAdminSession).filter(
            TenantAdminSession.id == session_id
        ).first()
        if not session_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if not may_act_on_tenant(current_admin, admin_type, session_row.tenant_id):
            # Don't leak the existence of cross-tenant sessions.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if not session_row.is_active:
            # Idempotent — revoking an already-dead session is a no-op,
            # not an error. Saves the UI from dealing with races where
            # two admins click Revoke at nearly the same time.
            return {
                "ok": True,
                "already_inactive": True,
                "session_id": session_row.id,
            }

        session_row.deactivate("revoked_by_admin")
        db.flush()

        audit.log(
            action="tenant_admin_session_revoked",
            admin=current_admin if admin_type == AdminType.TENANT_ADMIN_TYPE.value else None,
            admin_type=AdminType.TENANT_ADMIN_TYPE,
            tenant_id=session_row.tenant_id,
            success=True,
            resource_type="admin_session",
            resource_id=session_row.id,
            details={
                "revoked_admin_id": session_row.admin_id,
                "revoked_by_admin_id": current_admin.id,
                "revoked_by_admin_type": admin_type,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        return {
            "ok": True,
            "session_id": session_row.id,
            "deactivated_at": session_row.deactivated_at.isoformat() if session_row.deactivated_at else None,
        }

    @router.get(
        "/tenant/{admin_id}",
        response_model=AdminResponse,
        summary="Get Tenant Admin",
        description="""
        Get details of a specific tenant administrator.

        - System admins: Can view any tenant admin
        - Tenant admins: Can only view admins from their own tenant
        """
    )
    def get_tenant_admin(
            request: Request,
            admin_id: str,
            db: Session = Depends(get_db),
            current_admin_and_type=Depends(get_current_admin),
            _: Any = Depends(require_permission(Permission.ADMIN_USER_READ))
    ) -> AdminResponse:
        """Get tenant administrator details"""
        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        try:
            # Get the target admin
            target_admin = db.query(TenantAdmin).filter(
                TenantAdmin.id == admin_id
            ).first()

            if not target_admin:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Tenant admin '{admin_id}' not found"
                )

            # Tenant admins can only view admins from their own tenant
            if not may_act_on_tenant(current_admin, admin_type, target_admin.tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot view admin from another tenant"
                )

            logger.debug("Retrieved tenant admin details", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "tenant_id": target_admin.tenant_id
            })

            return AdminResponse(**target_admin.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get tenant admin", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get tenant admin: {str(e)}"
            )

    @router.patch(
        "/tenant/{admin_id}",
        response_model=AdminResponse,
        summary="Update Tenant Admin",
        description="""
        Update a tenant administrator's information.

        - System admins: Can update any tenant admin
        - Tenant admins: Can only update admins from their own tenant
        """
    )
    def update_tenant_admin(
            request: Request,
            admin_id: str,
            body: UpdateTenantAdminRequest,
            db: Session = Depends(get_db),
            current_admin_and_type=Depends(get_current_admin),
            auth_service: UnifiedAuthService = Depends(get_auth_service),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            _: Any = Depends(require_permission(Permission.ADMIN_USER_UPDATE))
    ) -> AdminResponse:
        """Update tenant administrator"""
        current_admin, admin_type = current_admin_and_type
        update_data = body.model_dump(exclude_unset=True)

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        try:
            # Get the target admin
            target_admin = db.query(TenantAdmin).filter(
                TenantAdmin.id == admin_id
            ).first()

            if not target_admin:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Tenant admin '{admin_id}' not found"
                )

            # Tenant admins can only update admins from their own tenant
            if not may_act_on_tenant(current_admin, admin_type, target_admin.tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot update admin from another tenant"
                )

            # Self password change must go through the dedicated endpoint
            # (POST /v1/admin/users/me/change-password) that requires the
            # current password. Reject it here so a hijacked session —
            # or a UI foot-gun — cannot silently change the caller's own
            # password and lock them out.
            if target_admin.id == current_admin_id and 'password' in update_data:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Use POST /v1/admin/users/me/change-password to change your own password.",
                )

            # Update allowed fields
            allowed_fields = ['email', 'full_name', 'is_active']
            updated_fields = {}

            for field in allowed_fields:
                if field in update_data:
                    setattr(target_admin, field, update_data[field])
                    updated_fields[field] = update_data[field]

            # Update password if provided. For another admin only —
            # self-updates were rejected above.
            if update_data.get('password') is not None:
                target_admin.set_password(update_data['password'])
                updated_fields['password'] = '***'  # Don't log actual password
                deactivate_admin_sessions(db, target_admin, "password_reset_by_admin")

            target_admin.updated_at = datetime.now(timezone.utc)
            target_admin.updated_by = current_admin_id

            db.flush()
            db.refresh(target_admin)

            # Audit log
            audit.log(
                action="tenant_admin_updated",
                admin=current_admin,
                admin_type=admin_type,
                tenant_id=target_admin.tenant_id,
                success=True,
                resource_type="tenant_admin",
                resource_id=admin_id,
                details={
                    "updated_admin_id": admin_id,
                    "updated_fields": updated_fields
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Tenant admin updated", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "updated_fields": list(updated_fields.keys())
            })

            return AdminResponse(**target_admin.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update tenant admin", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update tenant admin: {str(e)}"
            )

    @router.delete(
        "/tenant/{admin_id}",
        status_code=status.HTTP_200_OK,
        summary="Delete Tenant Admin",
        description="""
        Delete (or deactivate) a tenant administrator.

        - System admins: Can delete any tenant admin
        - Tenant admins: Can only delete admins from their own tenant

        Note: This performs a soft delete (deactivation) by default for audit compliance.
        """
    )
    def delete_tenant_admin(
            request: Request,
            admin_id: str,
            hard_delete: bool = False,  # Optional: allow hard delete for system admins
            db: Session = Depends(get_db),
            current_admin_and_type=Depends(get_current_admin),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            _: Any = Depends(require_permission(Permission.ADMIN_USER_DELETE))
    ) -> Dict[str, Any]:
        """Delete or deactivate tenant administrator"""
        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        try:
            # Get the target admin
            target_admin = db.query(TenantAdmin).filter(
                TenantAdmin.id == admin_id
            ).first()

            if not target_admin:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Tenant admin '{admin_id}' not found"
                )

            # Eager load target admin attributes
            target_admin_id = target_admin.id
            target_admin_username = target_admin.username
            target_admin_tenant_id = target_admin.tenant_id

            # Tenant admins can only delete admins from their own tenant
            if not may_act_on_tenant(current_admin, admin_type, target_admin_tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot delete admin from another tenant"
                )

            # Prevent self-deletion
            if target_admin_id == current_admin_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot delete yourself"
                )

            # Hard delete only allowed for system admins
            if hard_delete and not is_platform_wide(admin_type):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only system admins can perform hard deletes"
                )

            deletion_type = "hard" if hard_delete else "soft"

            if hard_delete:
                # Hard delete - actually remove from database
                db.delete(target_admin)
            else:
                # Soft delete - just deactivate
                target_admin.is_active = False
                target_admin.updated_at = datetime.now(timezone.utc)
                target_admin.updated_by = current_admin_id

            db.flush()

            # Audit log
            audit.log(
                action=f"tenant_admin_{deletion_type}_deleted",
                admin=current_admin,
                admin_type=admin_type,
                tenant_id=target_admin_tenant_id,
                success=True,
                resource_type="tenant_admin",
                resource_id=target_admin_id,
                details={
                    "deleted_admin_id": target_admin_id,
                    "deleted_username": target_admin_username,
                    "deletion_type": deletion_type
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info(f"Tenant admin {deletion_type} deleted", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "deletion_type": deletion_type
            })

            return {
                "message": f"Tenant admin {'deleted' if hard_delete else 'deactivated'} successfully",
                "admin_id": admin_id,
                "deletion_type": deletion_type
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete tenant admin", context={
                "requester_id": current_admin_id,
                "target_admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete tenant admin: {str(e)}"
            )

    @router.post(
        "/tenant/{admin_id}/unlock",
        response_model=AdminResponse,
        summary="Unlock Tenant Admin",
        description="""
        Lift the lock on a tenant administrator's account: the lock, its end
        and the count of failed attempts are cleared, so the administrator
        can sign in again. (A lock stops new sign-ins only; sessions that
        were open went on working.)

        - Tenant admins: administrators of their own tenant
        - System admins: any tenant admin

        An account that is not locked is left as it is (200). The action is
        recorded in the audit trail either way.
        """,
        responses={
            401: {"description": "Not signed in"},
            403: {"description": "Without the permission, or the administrator belongs to another tenant"},
            404: {"description": "No such tenant administrator"},
        },
    )
    def unlock_tenant_admin(
            request: Request,
            admin_id: str,
            db: Session = Depends(get_db),
            current_admin_and_type=Depends(get_current_admin),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            _: Any = Depends(require_permission(Permission.ADMIN_USER_UPDATE))
    ) -> AdminResponse:
        """Unlock a tenant administrator"""
        current_admin, admin_type = current_admin_and_type

        # Before anything is looked up: a caller that is no registered
        # principal is refused here (403), and learns nothing about the id.
        tenant_scope(current_admin, admin_type)

        target_admin = db.query(TenantAdmin).filter(TenantAdmin.id == admin_id).first()
        if not target_admin:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant admin '{admin_id}' not found"
            )
        if not may_act_on_tenant(current_admin, admin_type, target_admin.tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot unlock admin from another tenant"
            )

        details = {
            "unlocked_admin_id": target_admin.id,
            "unlocked_username": target_admin.username,
            "was_locked": lockout.currently_locked(target_admin),
            "failed_login_attempts": target_admin.failed_login_attempts or 0,
        }
        lockout.clear(target_admin)
        target_admin.updated_at = datetime.now(timezone.utc)
        db.flush()
        db.refresh(target_admin)

        audit.log(
            action="tenant_admin_unlocked",
            admin=current_admin,
            admin_type=admin_type,
            tenant_id=target_admin.tenant_id,
            success=True,
            resource_type="tenant_admin",
            resource_id=target_admin.id,
            details=details,
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )
        logger.info("Tenant admin unlocked", context={
            "requester_id": current_admin.id,
            "target_admin_id": target_admin.id,
            "was_locked": details["was_locked"],
        })
        return AdminResponse(**target_admin.to_dict())

    @router.get(
        "/audit-logs",
        response_model=Dict[str, Any],
        summary="Get Admin user Audit Logs",
        description="""
            Retrieve audit logs for the current tenant's admin actions.

            **URL**: `/v1/admin/users/audit-logs`

            **Access Control**:
            - Tenant admins can only see audit logs for their own tenant
            - System admins can view logs for all tenants

            **Query Parameters**:
            - `limit`: Maximum number of logs to return (default: 100, max: 1000)
            - `user_filter`: Filter by admin_id
            - `action_filter`: Filter by action type (partial match)
            - `success_only`: Filter by success status (true/false)

            **Returns**: Tenant admin audit logs from `tenant_admin_audit_logs` table
            **NOT**: MCP proxy transport logs
            """
    )
    def get_tenant_audit_logs(
            limit: int = Query(100, ge=1, le=1000, description="Maximum number of logs to return"),
            user_filter: Optional[str] = Query(None, description="Filter by admin ID"),
            action_filter: Optional[str] = Query(None, description="Filter by action type"),
            success_only: Optional[bool] = Query(None, description="Filter by success status"),
            db: Session = Depends(get_db),
            current_admin: tuple = Depends(get_current_admin),
            _: Any = Depends(require_permission(Permission.ADMIN_AUDIT_READ))
    ):
        """
        Get audit logs for tenant admin actions

        This retrieves logs from the tenant_admin_audit_logs table,
        NOT from the MCP proxy transport logs.
        """
        admin, admin_type = current_admin

        # Eager load admin attributes for exception handlers and logging
        admin_id = admin.id
        admin_tenant_id = admin.tenant_id if hasattr(admin, 'tenant_id') else None

        try:
            # Build query based on admin type
            query = db.query(TenantAdminAuditLog)

            # Tenant admins can only see their own tenant's logs
            # The system administrator sees every tenant's; a type nobody
            # registered sees none (403).
            scope_tenant_id = tenant_scope(admin, admin_type)
            if scope_tenant_id:
                query = query.filter(TenantAdminAuditLog.tenant_id == scope_tenant_id)
                logger.debug(f"Tenant admin {admin_id} fetching audit logs for tenant {scope_tenant_id}")
            else:
                logger.debug(f"System admin {admin_id} fetching all tenant audit logs")

            # Apply filters
            if user_filter:
                query = query.filter(TenantAdminAuditLog.admin_id == user_filter)
                logger.debug(f"Filtering by admin_id: {user_filter}")

            if action_filter:
                query = query.filter(TenantAdminAuditLog.action.ilike(f"%{action_filter}%"))
                logger.debug(f"Filtering by action: {action_filter}")

            if success_only is not None:
                query = query.filter(TenantAdminAuditLog.success == success_only)
                logger.debug(f"Filtering by success: {success_only}")

            # Order by timestamp descending (newest first) and apply limit
            query = query.order_by(desc(TenantAdminAuditLog.timestamp))
            query = query.limit(limit)

            # Execute query
            logs = query.all()

            # Convert to dict format
            logs_data = [log.to_dict() for log in logs]

            logger.debug(f"Retrieved {len(logs_data)} tenant audit logs (limit: {limit})")

            return {
                "logs": logs_data,
                "count": len(logs_data),
                "limit": limit,
                "filters_applied": {
                    "user_filter": user_filter,
                    "action_filter": action_filter,
                    "success_only": success_only
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching tenant audit logs: {str(e)}", exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch audit logs: {str(e)}"
            )

    # ----------------------------------------------------------------
    # Admin Audit Logs — facets + typeahead (Datadog-style discovery)
    #
    # Mirror of the proxy-logs facets endpoint, scoped to
    # `tenant_admin_audit_logs`. The audit-log volume is far lower
    # than proxy_logs (hundreds of rows per tenant per day, not
    # millions), so we use a single round-trip GROUP BY without an
    # intermediate subquery — fast enough, simpler to read.
    #
    # Facetable columns: admin_id, action, resource_type, success,
    # ip_address. The free-text `q` searches across action +
    # resource_type + resource_id + ip_address + request_path
    # (matches the client-side filtering AuditLogViewer was doing).
    # ----------------------------------------------------------------

    _AUDIT_FACETABLE = {
        "admin_id":      "Admin",
        "action":        "Action",
        "resource_type": "Resource type",
        "success":       "Status",
        "ip_address":    "IP address",
        "request_method": "HTTP method",
    }

    def _audit_scope_query(db_: Session, admin_, admin_type_, body: Dict[str, Any]):
        """Build the WHERE-only audit log query shared by
        /audit-logs/facets and /audit-logs/suggest. Tenant admins
        are pinned to their own tenant; system admins can pass
        `tenant_id` to scope to a single tenant view.

        `body` keys: tenant_id (sysadmin only), admin_ids (list),
        actions (list), resource_types (list), success (bool),
        start_time, end_time, q.
        """
        q = db_.query(TenantAdminAuditLog)
        # A tenant administrator's own tenant, whatever the body names; the
        # system administrator's choice, or every tenant; a type nobody
        # registered: 403.
        scope_tenant_id = tenant_scope(
            admin_, admin_type_, body.get("tenant_id") if is_platform_wide(admin_type_) else None
        )
        if scope_tenant_id:
            q = q.filter(TenantAdminAuditLog.tenant_id == scope_tenant_id)

        return _apply_audit_filters(q, TenantAdminAuditLog, body)

    def _apply_audit_filters(q, trail, body: Dict[str, Any]):
        """The filters of the audit log, for the list, its facets and the
        download alike, on `trail` (the tenant administrators' audit log,
        or a platform-wide principal's own)."""
        if body.get("admin_ids"):
            q = q.filter(trail.admin_id.in_(body["admin_ids"]))
        if body.get("actions"):
            q = q.filter(trail.action.in_(body["actions"]))
        if body.get("resource_types"):
            q = q.filter(trail.resource_type.in_(body["resource_types"]))
        if body.get("ip_addresses"):
            q = q.filter(trail.ip_address.in_(body["ip_addresses"]))
        if body.get("request_methods"):
            q = q.filter(trail.request_method.in_(body["request_methods"]))
        if body.get("success") is not None:
            q = q.filter(trail.success == body["success"])
        # The time range: `hours_ago` (a preset) or `start_time`/`end_time`.
        if body.get("hours_ago"):
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=float(body["hours_ago"]))
                q = q.filter(trail.timestamp >= cutoff)
            except (TypeError, ValueError):
                pass
        if body.get("start_time"):
            q = q.filter(trail.timestamp >= body["start_time"])
        if body.get("end_time"):
            q = q.filter(trail.timestamp <= body["end_time"])
        free_text = body.get("q")
        if free_text:
            like = f"%{free_text}%"
            q = q.filter(
                sa_or(
                    trail.action.ilike(like),
                    trail.resource_type.ilike(like),
                    trail.resource_id.ilike(like),
                    trail.ip_address.ilike(like),
                    trail.request_path.ilike(like),
                )
            )
        return q

    @router.post(
        "/audit-logs/search",
        summary="Admin audit logs — multi-value POST search (Datadog-style)",
        description=(
            "POST mirror of the GET /audit-logs endpoint, accepting "
            "the same Datadog-style filter envelope as /audit-logs/facets. "
            "Filters are OR-within-field, AND-across-fields. Sortable, "
            "paginated, supports `q` free-text search. Existing GET "
            "stays for legacy callers; new clients should target this "
            "endpoint."
        ),
    )
    def search_audit_logs(
        body: Dict[str, Any],
        db: Session = Depends(get_db),
        current_admin: tuple = Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.ADMIN_AUDIT_READ)),
    ):
        admin_obj, admin_type = current_admin
        try:
            q = _audit_scope_query(db, admin_obj, admin_type, body)
            total = q.count()
            limit = min(int(body.get("limit", 100)), 1000)
            offset = int(body.get("offset", 0))
            q = q.order_by(desc(TenantAdminAuditLog.timestamp))
            logs = q.limit(limit).offset(offset).all()
            return {
                "logs": [log.to_dict() for log in logs],
                "count": len(logs),
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search audit logs: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to search audit logs: {str(e)}",
            )

    @router.post(
        "/audit-logs/facets",
        summary="Top-N values + counts per facetable column for admin audit logs",
        description=(
            "Returns {field: [{value, count}, ...]} for every facetable "
            "column scoped to the active filter envelope. Used by the "
            "Admin Audit Logs UI's FacetMultiSelect dropdowns and the "
            "Datadog-style faceted sidebar."
        ),
    )
    def get_audit_facets(
        body: Dict[str, Any],
        db: Session = Depends(get_db),
        current_admin: tuple = Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.ADMIN_AUDIT_READ)),
    ):
        admin_obj, admin_type = current_admin
        try:
            fields = body.get("fields") or list(_AUDIT_FACETABLE.keys())
            fields = [f for f in fields if f in _AUDIT_FACETABLE]
            per_field_limit = int(body.get("per_field_limit", 50))

            base_subq = _audit_scope_query(db, admin_obj, admin_type, body).subquery()

            out: Dict[str, List[Dict[str, Any]]] = {}
            for field in fields:
                col = getattr(base_subq.c, field, None)
                if col is None:
                    continue
                rows = (
                    db.query(col.label("value"), func.count().label("count"))
                    .filter(col.isnot(None))
                    .group_by(col)
                    .order_by(func.count().desc())
                    .limit(per_field_limit)
                    .all()
                )
                out[field] = [{"value": r.value, "count": r.count} for r in rows]
            return {"facets": out}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to compute audit facets: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compute audit facets: {str(e)}",
            )

    @router.post(
        "/audit-logs/suggest",
        summary="Typeahead for a single facetable admin-audit column",
        description=(
            "Prefix-matched values for the named column, scoped to the "
            "active filter envelope. Same shape as the proxy_logs /suggest "
            "endpoint."
        ),
    )
    def get_audit_suggestions(
        body: Dict[str, Any],
        db: Session = Depends(get_db),
        current_admin: tuple = Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.ADMIN_AUDIT_READ)),
    ):
        admin_obj, admin_type = current_admin
        field = body.get("field")
        prefix = body.get("prefix", "")
        suggest_limit = int(body.get("suggest_limit", 20))
        if field not in _AUDIT_FACETABLE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"field {field!r} is not facetable",
            )
        if not prefix:
            return {"suggestions": []}
        try:
            base_subq = _audit_scope_query(db, admin_obj, admin_type, body).subquery()
            col = getattr(base_subq.c, field, None)
            if col is None:
                return {"suggestions": []}
            like = f"{prefix}%"
            rows = (
                db.query(col.label("value"), func.count().label("count"))
                .filter(col.isnot(None))
                .filter(col.ilike(like))
                .group_by(col)
                .order_by(func.count().desc())
                .limit(suggest_limit)
                .all()
            )
            return {
                "suggestions": [{"value": r.value, "count": r.count} for r in rows]
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to compute audit suggestions: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compute audit suggestions: {str(e)}",
            )

    @router.get(
        "/audit-logs/export",
        summary="Export admin audit logs (CSV or JSONL, streamed)",
        description="""
            Stream admin audit log entries as CSV or JSONL with a self-describing
            metadata header.

            **URL**: `/v1/admin/users/audit-logs/export`

            **Access Control**:
            - Tenant admins receive only their own tenant's audit logs (from
              `tenant_admin_audit_logs`).
            - System admins receive system-admin actions (their own
              audit trail); they may also pass
              `tenant_id` to scope a tenant view.

            **Output**:
            - `format=csv` → comment-prefixed metadata, then header, then rows.
            - `format=jsonl` → first line `{"_meta": ...}`, then one record
              per line.

            **Filters**: the filters of the list (`POST /audit-logs/search`),
            as query parameters with the same meaning: `admin_ids`, `actions`,
            `resource_types`, `ip_addresses`, `request_methods` (each may be
            repeated, and each value is matched exactly), `success`,
            `hours_ago`, `start_time`, `end_time`, `q`. The file holds the
            rows the filtered list shows.

            Synchronous cap: 50,000 rows. Each invocation is recorded in the
            admin audit trail (`action = "export_audit_logs"`).
            """
    )
    def export_audit_logs(
            request: Request,
            format: Literal["csv", "jsonl"] = Query("csv", description="Output format"),
            tenant_id: Optional[str] = Query(
                None,
                description="System admins only: scope to a tenant's audit log"
            ),
            start_time: Optional[datetime] = Query(None),
            end_time: Optional[datetime] = Query(None),
            admin_ids: Optional[List[str]] = Query(None, description="Administrators (repeat the parameter)"),
            actions: Optional[List[str]] = Query(None, description="Actions, exact (repeat the parameter)"),
            resource_types: Optional[List[str]] = Query(None, description="Resource types (repeat the parameter)"),
            ip_addresses: Optional[List[str]] = Query(None, description="IP addresses (repeat the parameter)"),
            request_methods: Optional[List[str]] = Query(None, description="HTTP methods (repeat the parameter)"),
            success: Optional[bool] = Query(None, description="Outcome"),
            hours_ago: Optional[float] = Query(None, gt=0, description="Only rows of the last N hours"),
            q: Optional[str] = Query(None, description="Free text: action, resource, IP address, path"),
            db: Session = Depends(get_db),
            current_admin: tuple = Depends(get_current_admin),
            audit_logger: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            _: Any = Depends(require_permission(Permission.ADMIN_AUDIT_READ))
    ):
        """Stream audit logs as CSV or JSONL with metadata + audit trail entry."""
        admin, admin_type = current_admin
        admin_id = admin.id
        admin_username = getattr(admin, 'username', 'unknown')
        admin_tenant_id = admin.tenant_id if hasattr(admin, 'tenant_id') else None

        # Decide which audit log table to read.
        # Tenant admin → their tenant's tenant_admin_audit_logs (forced).
        # A principal without a tenant (the Enterprise Edition's system
        #   administrator) → its own trail by default; if tenant_id was
        #   passed, that tenant's tenant_admin_audit_logs instead. This
        #   module names no table of that principal.
        # Asked of the principal: a type nobody registered is refused.
        scope_tenant_id = tenant_scope(admin, admin_type, tenant_id)
        scope = "tenant" if scope_tenant_id else "system"
        # The filters of the list (`POST /audit-logs/search`), with the
        # same meaning.
        list_filters = {
            "admin_ids": admin_ids, "actions": actions, "resource_types": resource_types,
            "ip_addresses": ip_addresses, "request_methods": request_methods, "success": success,
            "hours_ago": hours_ago, "start_time": start_time, "end_time": end_time, "q": q,
        }

        try:
            if scope == "tenant":
                base = db.query(TenantAdminAuditLog).filter(
                    TenantAdminAuditLog.tenant_id == scope_tenant_id
                )
                base = _apply_audit_filters(base, TenantAdminAuditLog, list_filters)
                total = base.count()
                assert_within_cap(total, resource="audit log rows")
                rows = base.order_by(desc(TenantAdminAuditLog.timestamp)).all()
            else:
                # No tenant: a platform-wide principal reading its own trail.
                # Which table that is, is the principal's to say;
                # one without a trail of its own is refused.
                principal = PRINCIPALS.get(admin_type)
                if principal is None or principal.audit_log_model is None:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="No audit trail for this administrator type",
                    )
                trail = principal.audit_log_model()
                base = db.query(trail)
                base = _apply_audit_filters(base, trail, list_filters)
                total = base.count()
                assert_within_cap(total, resource="audit log rows")
                rows = base.order_by(desc(trail.timestamp)).all()

            log_dicts = [r.to_dict() for r in rows]
            row_count = len(log_dicts)

            filters = {
                "scope": scope,
                "tenant_id": scope_tenant_id,
                **list_filters,
            }

            meta = build_meta(
                resource="audit_logs",
                format_type=format,
                exported_by=f"{admin_username} ({admin_id})",
                filters=filters,
                row_count=row_count,
            )

            try:
                audit_logger.log(
                    action="export_audit_logs",
                    db=db,
                    admin=admin,
                    success=True,
                    resource_type="admin_audit_logs",
                    tenant_id=admin_tenant_id,
                    affected_tenant_id=scope_tenant_id,
                    details={
                        "format": format,
                        "row_count": row_count,
                        "scope": scope,
                        "filters": meta["filters"],
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    request_method=request.method,
                    request_path=str(request.url.path),
                )
            except Exception as e:
                logger.warning(
                    f"Failed to record audit log export in audit trail: {e}"
                )

            filename = export_filename(
                "audit-logs", format,
                start_time=start_time, end_time=end_time,
            )
            response_headers = {
                "Content-Disposition": f"attachment; filename={filename}"
            }

            if format == "jsonl":
                generator = stream_jsonl(records=log_dicts, meta=meta)
            else:
                generator = stream_csv(
                    records=log_dicts,
                    headers=AUDIT_LOG_CSV_HEADERS,
                    meta=meta,
                    row_extractor=flatten_audit_log,
                )

            return StreamingResponse(
                generator,
                media_type=media_type_for(format),
                headers=response_headers,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error exporting audit logs: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to export audit logs: {str(e)}"
            )

    # =========================================================================
    # COMMON ENDPOINTS (work for both admin types)
    # =========================================================================

    @router.post(
        "/logout",
        response_model=LogoutResponse,
        summary="Logout",
        description="Logout and invalidate current session (works for both admin types)"
    )
    def logout(
        request: Request,
        db: Session = Depends(get_db),
        current_admin_and_type = Depends(get_current_admin),
        auth_service: UnifiedAuthService = Depends(get_auth_service),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Logout current admin"""

        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        # Extract token from Authorization header
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header"
            )

        token = auth_header.split(' ')[1]

        success = auth_service.logout(token, db)

        if success:

            audit.log(
                action=f"{admin_type}_admin_logout",
                admin=current_admin,
                admin_type=admin_type,
                tenant_id=current_admin.tenant_id if admin_type == AdminType.TENANT_ADMIN_TYPE.value else None,
                success=True,
                resource_type="auth",
                resource_id=current_admin_id,
                details={"admin_type": admin_type},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info(f"{admin_type.title()} admin logged out", context={
                "admin_id": current_admin_id,
                "username": current_admin_username
            })

        return LogoutResponse(message="Successfully logged out")

    @router.get(
        "/me",
        response_model=CurrentAdminResponse,
        summary="Get Current Admin",
        description=(
            "The authenticated administrator, whether the installation has a system "
            "administrator, and whether the caller operates what the installation shares."
        ),
    )
    async def get_current_admin_info(
        current_admin_and_type = Depends(get_current_admin)
    ):
        """Get current admin information"""

        current_admin, admin_type = current_admin_and_type

        # Eager load current admin attributes for exception handlers
        current_admin_id = current_admin.id
        current_admin_username = current_admin.username
        current_admin_tenant_id = current_admin.tenant_id if hasattr(current_admin, 'tenant_id') else None

        return CurrentAdminResponse(
            **current_admin.to_dict(),
            installation_has_system_admin=PRINCIPALS.has_platform_wide(),
            is_installation_operator=is_installation_operator(admin_type),
        )


    # =========================================================================
    # ROLE MANAGEMENT ENDPOINTS (USER->ROLE->PERMISSIONS)
    # =========================================================================

    @router.get(
        "/roles",
        response_model=RoleListResponse,
        summary="List all roles",
        description="List all roles for the current tenant"
    )
    def list_roles(
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        _: Any = Depends(require_permission(Permission.ROLES_READ))
    ):
        """List all roles for the current tenant"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Query roles
        roles = db.query(TenantRole).filter(
            TenantRole.tenant_id == tenant_id
        ).order_by(TenantRole.is_system.desc(), TenantRole.name).all()

        # Build response
        role_responses = []
        for role in roles:
            permissions = role.get_permissions()
            role_responses.append(RoleResponse(
                id=role.id,
                tenant_id=role.tenant_id,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
                permissions=permissions,
                permission_count=len(permissions),
                created_at=role.created_at.isoformat(),
                updated_at=role.updated_at.isoformat() if role.updated_at else None
            ))

        system_count = sum(1 for r in roles if r.is_system)
        custom_count = len(roles) - system_count

        return RoleListResponse(
            roles=role_responses,
            total=len(roles),
            system_roles=system_count,
            custom_roles=custom_count
        )

    @router.get(
        "/roles/{role_id}",
        response_model=RoleResponse,
        summary="Get role details",
        description="Get detailed information about a specific role"
    )
    def get_role(
        role_id: str,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        _: Any = Depends(require_permission(Permission.ROLES_READ))
    ):
        """Get details of a specific role"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Get role
        role = db.query(TenantRole).filter(
            TenantRole.id == role_id,
            TenantRole.tenant_id == tenant_id
        ).first()

        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        permissions = role.get_permissions()

        return RoleResponse(
            id=role.id,
            tenant_id=role.tenant_id,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=permissions,
            permission_count=len(permissions),
            created_at=role.created_at.isoformat(),
            updated_at=role.updated_at.isoformat() if role.updated_at else None
        )

    @router.post(
        "/roles",
        response_model=RoleResponse,
        summary="Create custom role",
        description="Create a new custom role with specified permissions"
    )
    def create_role(
        role_request: CreateRoleRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ROLES_CREATE))
    ):
        """Create a new custom role"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Check if role name already exists
        existing = db.query(TenantRole).filter(
            TenantRole.tenant_id == tenant_id,
            TenantRole.name == role_request.name
        ).first()

        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Role '{role_request.name}' already exists"
            )

        # Validate permissions
        valid_permissions = set(PERMISSIONS.values())

        # SYSTEM_ADMIN can only be granted by other SYSTEM_ADMIN holders
        if Permission.SYSTEM_ADMIN.value in role_request.permissions:
            if not admin.has_permission(Permission.SYSTEM_ADMIN.value):
                raise HTTPException(
                    status_code=403,
                    detail="Only system administrators can grant system:admin permission"
                )

        for perm in role_request.permissions:
            if perm not in valid_permissions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid permission: {perm}"
                )

        # Create role
        role_id = f"role-{uuid4().hex[:12]}"
        role = TenantRole(
            id=role_id,
            tenant_id=tenant_id,
            name=role_request.name,
            description=role_request.description,
            is_system=False,  # Custom roles are not system roles
            created_at=datetime.now(timezone.utc)
        )

        db.add(role)
        db.flush()

        # Add permissions
        for perm in role_request.permissions:
            perm_id = f"perm-{uuid4().hex[:12]}"
            role_perm = TenantRolePermission(
                id=perm_id,
                role_id=role.id,
                permission=perm,
                created_at=datetime.now(timezone.utc)
            )
            db.add(role_perm)

        db.flush()
        db.refresh(role)

        # Audit log
        audit.log(
            action="role_created",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="role",
            resource_id=role.id,
            details={
                "role_id": role.id,
                "role_name": role.name,
                "permissions": role_request.permissions,
                "permissions_count": len(role_request.permissions),
                "description": role.description
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Custom role created", context={
            "role_id": role.id,
            "role_name": role.name,
            "tenant_id": tenant_id,
            "permissions_count": len(role_request.permissions),
            "created_by": admin.username
        })

        return RoleResponse(
            id=role.id,
            tenant_id=role.tenant_id,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=role.get_permissions(),
            permission_count=len(role_request.permissions),
            created_at=role.created_at.isoformat(),
            updated_at=None
        )

    @router.patch(
        "/roles/{role_id}",
        response_model=RoleResponse,
        summary="Update role",
        description="Update a custom role's name, description, or permissions"
    )
    def update_role(
        role_id: str,
        role_update: UpdateRoleRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ROLES_UPDATE))
    ):
        """Update an existing role"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Get role
        role = db.query(TenantRole).filter(
            TenantRole.id == role_id,
            TenantRole.tenant_id == tenant_id
        ).first()

        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        # Cannot modify system roles
        if role.is_system:
            raise HTTPException(
                status_code=403,
                detail="Cannot modify system roles"
            )

        # roles:update must not let its holder add to the role they hold.
        if role_update.permissions is not None and getattr(admin, "role_id", None) == role.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot change the permissions of your own role"
            )

        # Track what changed
        updated_fields = {}
        old_values = {}

        # Update fields
        if role_update.name is not None:
            # Check name uniqueness
            existing = db.query(TenantRole).filter(
                TenantRole.tenant_id == tenant_id,
                TenantRole.name == role_update.name,
                TenantRole.id != role_id
            ).first()

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Role name '{role_update.name}' already exists"
                )

            old_values["name"] = role.name
            role.name = role_update.name
            updated_fields["name"] = role_update.name

        if role_update.description is not None:
            old_values["description"] = role.description
            role.description = role_update.description
            updated_fields["description"] = role_update.description

        if role_update.permissions is not None:
            # Validate permissions
            valid_permissions = set(PERMISSIONS.values())

            # Check SYSTEM_ADMIN permission
            if Permission.SYSTEM_ADMIN.value in role_update.permissions:
                if not admin.has_permission(Permission.SYSTEM_ADMIN.value):
                    raise HTTPException(
                        status_code=403,
                        detail="Only system administrators can grant system:admin permission"
                    )

            for perm in role_update.permissions:
                if perm not in valid_permissions:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid permission: {perm}"
                    )

            # Track old permissions
            old_values["permissions"] = role.get_permissions()

            # Delete existing permissions
            db.query(TenantRolePermission).filter(
                TenantRolePermission.role_id == role.id
            ).delete()

            # Add new permissions
            from uuid import uuid4
            for perm in role_update.permissions:
                perm_id = f"perm-{uuid4().hex[:12]}"
                role_perm = TenantRolePermission(
                    id=perm_id,
                    role_id=role.id,
                    permission=perm,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(role_perm)

            updated_fields["permissions"] = role_update.permissions
            updated_fields["permissions_count"] = len(role_update.permissions)

        role.updated_at = datetime.now(timezone.utc)
        db.flush()
        db.refresh(role)

        # Audit log
        audit.log(
            action="role_updated",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="role",
            resource_id=role.id,
            details={
                "role_id": role.id,
                "role_name": role.name,
                "updated_fields": updated_fields,
                "old_values": old_values
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Role updated", context={
            "role_id": role.id,
            "role_name": role.name,
            "tenant_id": tenant_id,
            "updated_fields": list(updated_fields.keys()),
            "updated_by": admin.username
        })

        return RoleResponse(
            id=role.id,
            tenant_id=role.tenant_id,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=role.get_permissions(),
            permission_count=len(role.get_permissions()),
            created_at=role.created_at.isoformat(),
            updated_at=role.updated_at.isoformat() if role.updated_at else None
        )

    @router.delete(
        "/roles/{role_id}",
        summary="Delete custom role",
        description=(
            "Delete a custom role (system roles cannot be deleted). Refused with 409 while an "
            "administrator holds the role."
        ),
        responses={
            409: {
                **IN_USE_RESPONSE,
                "description": "An administrator holds the role; `detail.used_by.administrators` lists them.",
            }
        },
    )
    def delete_role(
        role_id: str,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ROLES_DELETE))
    ):
        """Delete a custom role"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Get role
        role = db.query(TenantRole).filter(
            TenantRole.id == role_id,
            TenantRole.tenant_id == tenant_id
        ).first()

        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        # Cannot delete system roles
        if role.is_system:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete system roles"
            )

        # Check if role is in use
        holders = db.query(TenantAdmin).filter(TenantAdmin.role_id == role_id).order_by(TenantAdmin.username).all()

        if holders:
            raise in_use(
                f"Cannot delete role: {len(holders)} admin(s) currently assigned to this role",
                administrators=holders,
            )

        # Store role info for audit log before deletion
        deleted_role_name = role.name
        deleted_role_permissions = role.get_permissions()

        # Delete role (permissions will cascade delete)
        db.delete(role)
        db.flush()

        # Audit log
        audit.log(
            action="role_deleted",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="role",
            resource_id=role_id,
            details={
                "role_id": role_id,
                "role_name": deleted_role_name,
                "permissions": deleted_role_permissions,
                "permissions_count": len(deleted_role_permissions)
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Role deleted", context={
            "role_id": role_id,
            "role_name": deleted_role_name,
            "tenant_id": tenant_id,
            "deleted_by": admin.username
        })

        return {"message": f"Role '{deleted_role_name}' deleted successfully"}

    @router.patch(
        "/tenant/{admin_id}/role",
        response_model=AdminResponse,
        summary="Assign role to admin",
        description="Assign a role to a tenant admin"
    )
    def assign_role_to_admin(
        admin_id: str,
        role_assignment: AssignRoleRequest,
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        db: Session = Depends(get_db),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _: Any = Depends(require_permission(Permission.ROLES_ASSIGN))
    ):
        """Assign a role to a tenant admin"""

        admin, admin_type = admin_and_type

        # Get tenant_id
        tenant_id = resolve_tenant_id(admin, admin_type, request.query_params.get('tenant_id'))

        # Get target admin
        target_admin = db.query(TenantAdmin).filter(
            TenantAdmin.id == admin_id,
            TenantAdmin.tenant_id == tenant_id
        ).first()

        if not target_admin:
            raise HTTPException(status_code=404, detail="Admin not found")

        # roles:assign must not let its holder raise their own permissions.
        if target_admin.id == admin.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot change your own role"
            )

        # Get role
        role = db.query(TenantRole).filter(
            TenantRole.id == role_assignment.role_id,
            TenantRole.tenant_id == tenant_id
        ).first()

        if not role:
            raise HTTPException(status_code=404, detail="Role not found")

        # A role that holds `system:admin` is handed out only by a caller
        # that holds it.
        refuse_role_beyond_the_caller(admin, role)

        # Store old role info for audit log
        old_role_id = target_admin.role_id
        old_role_name = None
        if old_role_id:
            old_role = db.query(TenantRole).filter(TenantRole.id == old_role_id).first()
            old_role_name = old_role.name if old_role else None

        # Assign role
        target_admin.role_id = role.id
        db.flush()
        db.refresh(target_admin)

        # Audit log
        audit.log(
            action="role_assigned",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="role_assignment",
            resource_id=admin_id,
            details={
                "target_admin_id": admin_id,
                "target_admin_username": target_admin.username,
                "new_role_id": role.id,
                "new_role_name": role.name,
                "old_role_id": old_role_id,
                "old_role_name": old_role_name
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        logger.info("Role assigned to admin", context={
            "admin_id": admin_id,
            "role_id": role.id,
            "role_name": role.name,
            "tenant_id": tenant_id,
            "assigned_by": admin.username
        })

        return AdminResponse(**target_admin.to_dict())

    @router.get(
        "/permissions",
        response_model=PermissionsListResponse,
        summary="List all available permissions",
        description="List all available permissions organized by category"
    )
    async def list_all_permissions(
        admin_and_type: tuple = Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.PERMISSIONS_READ))
    ):
        """List all available permissions with categories"""
        permissions_by_category: Dict[str, List[PermissionInfo]] = {}

        for spec in PERMISSIONS.specs():
            category = spec.category

            if category not in permissions_by_category:
                permissions_by_category[category] = []

            perm_info = PermissionInfo(
                name=spec.value,
                display_name=spec.value.replace("_", " ").replace(":", " ").title(),
                description=spec.description if spec.description is not None else f"Permission: {spec.value}",
                category=category
            )

            permissions_by_category[category].append(perm_info)

        return PermissionsListResponse(
            permissions_by_category=permissions_by_category,
            total_permissions=len(PERMISSIONS)
        )

    # ── Admin User Photo Upload / Serve / Delete ─────────────────

    def _photo_target_or_refuse(db_: Session, admin_id: str, current_admin, admin_type) -> TenantAdmin:
        """The admin whose photo this is, within the caller's reach. Your own
        photo needs no permission; anyone else's is an edit of that admin."""
        q = db_.query(TenantAdmin).filter(TenantAdmin.id == admin_id)
        if not is_platform_wide(admin_type):
            q = q.filter(TenantAdmin.tenant_id == current_admin.tenant_id)
        target = q.first()
        if not target:
            raise HTTPException(404, "Admin user not found")
        if target.id != current_admin.id and not current_admin.has_permission(
            Permission.ADMIN_USER_UPDATE.value
        ):
            raise HTTPException(403, "Permission denied")
        return target

    @router.post(
        "/tenant/{admin_id}/photo",
        summary="Upload admin user photo",
    )
    def upload_admin_photo(
        admin_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        import hashlib

        current_admin, admin_type = admin_and_type
        target = _photo_target_or_refuse(db, admin_id, current_admin, admin_type)

        form = block_on(request.form())
        file = form.get("file")
        if not file:
            raise HTTPException(400, "No file uploaded")

        data = block_on(file.read())
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "File too large (max 5MB)")

        content_type = file.content_type or "image/jpeg"
        if content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            raise HTTPException(400, f"Unsupported image type: {content_type}")

        photo_hash = hashlib.sha256(data).hexdigest()
        target.photo_data = data
        target.photo_mime_type = content_type
        target.photo_size = len(data)
        target.photo_hash = photo_hash
        # Full hash as the photo capability token (was truncated to 8 hex).
        # The public photo route requires this `?h=` token.
        target.avatar_url = f"/v1/assets/users/admin/{admin_id}/photo?h={photo_hash}"
        db.commit()

        audit.log(
            action="upload_admin_photo",
            admin=current_admin,
            admin_type=admin_type,
            tenant_id=target.tenant_id,
            success=True,
            resource_type="tenant_admin",
            resource_id=admin_id,
            details={
                "content_type": content_type,
                "photo_size": len(data),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        return {"success": True, "avatar_url": target.avatar_url}

    @router.delete("/tenant/{admin_id}/photo", summary="Delete admin user photo")
    def delete_admin_photo(
        admin_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        current_admin, admin_type = admin_and_type
        target = _photo_target_or_refuse(db, admin_id, current_admin, admin_type)

        target.photo_data = None
        target.photo_mime_type = None
        target.photo_size = None
        target.photo_hash = None
        target.avatar_url = None
        db.commit()

        audit.log(
            action="delete_admin_photo",
            admin=current_admin,
            admin_type=admin_type,
            tenant_id=target.tenant_id,
            success=True,
            resource_type="tenant_admin",
            resource_id=admin_id,
            details={"admin_id": admin_id},
            ip_address=client_ip(request),
            user_agent=request.headers.get('user-agent'),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db
        )

        return {"success": True}

    @router.get(
        "/tenant/{admin_id}/photo",
        summary="Get admin user photo (public, cacheable)"
    )
    def get_admin_photo(
        admin_id: str,
        request: Request,
        h: Optional[str] = Query(None, description="Photo capability token (prefix of photo_hash)"),
        db: Session = Depends(get_db),
    ):
        # Gated by the `?h=` capability token (prefix of photo_hash); canonical
        # route is `/v1/assets/...`. 404 when absent/mismatched.
        from fastapi.responses import Response
        from app.core.photo_token import photo_token_ok

        target = db.query(TenantAdmin).filter(TenantAdmin.id == admin_id).first()
        if not target or not target.photo_data or not photo_token_ok(target.photo_hash, h):
            raise HTTPException(404, "Photo not found")

        etag = target.photo_hash or ""
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304)

        return Response(
            content=target.photo_data,
            media_type=target.photo_mime_type or "image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "ETag": etag,
            },
        )

    return router