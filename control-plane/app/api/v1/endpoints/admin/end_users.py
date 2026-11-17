# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End User Management Endpoints (Admin) - Multi-Tenant with Unified Audit Logging
Allows administrators to view and manage end users with tenant isolation
"""

import io
import csv
from typing import Optional, Dict, Any, Union
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal

from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_current_admin,
    get_audit_logger
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.end_user.services.authentication import EndUserAuthentication
from app.api.v1.models.admin.end_user import UserDetailResponse, UserStatusUpdate, CreateEndUserRequest
from app.api.v1.models.admin.resource_group import SetPrimaryGroupRequest
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def create_end_user_router(
    end_user_auth: EndUserAuthentication,
    logger: BrutorLogger
) -> APIRouter:
    """
    Create end-user management router with multi-tenant support

    Tenant isolation:
    - Tenant admins: Only manage end users within their tenant
    - System admins: Can manage end users across tenants (must specify tenant_id)
    """
    router = APIRouter(
        prefix="/admin/end-users",
        tags=["End User Management (Admin)"]
    )

    @router.get(
        "",
        response_model=Dict[str, Any],
        summary="List all end users",
        description="""
        List end users.
        
        Tenant isolation:
        - Tenant admins: Only see their tenant's end users
        - System admins: See all end users or filter by tenant_id
        """
    )
    def list_end_users(
        request: Request,
        tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
        search: Optional[str] = Query(None, description="Search by username or email"),
        active_only: bool = Query(False, description="Filter active users only"),
        verified_only: bool = Query(False, description="Filter verified users only"),
        limit: int = Query(100, ge=1, le=1000, description="Max users to return"),
        offset: int = Query(0, ge=0, description="Pagination offset"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_READ))
    ):
        """List all end users with optional filtering and tenant isolation"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            users, total = block_on(end_user_auth.list_gateway_users(
                tenant_id=admin_tenant_id,
                search=search,
                active_only=active_only,
                verified_only=verified_only,
                limit=limit,
                offset=offset,
                db=db
            ))

            logger.info("End users listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "admin_username": admin_username,
                "tenant_id": admin_tenant_id,
                "total_users": len(users),
                "total_count": total,
                "search": search,
                "active_only": active_only,
                "verified_only": verified_only
            })

            return {
                "users": users,
                "pagination": {
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "returned": len(users)
                },
                "filters": {
                    "tenant_id": admin_tenant_id,
                    "search": search,
                    "active_only": active_only,
                    "verified_only": verified_only
                }
            }

        except Exception as e:
            # get_db() handles rollback automatically
            logger.error("Failed to list end users", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to list users: {str(e)}"
            )

    # ── Create End User ────────────────────────────────────────────

    @router.post(
        "",
        response_model=Dict[str, Any],
        summary="Create end user",
        description="Create a new end user from the admin console. Optionally sends a welcome email."
    )
    def create_end_user(
        req: CreateEndUserRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Create a new end user within the admin's tenant."""
        from app.database.models.end_user import EndUser
        from app.database.models.resource_group_membership import ResourceGroupMembership
        import bcrypt

        admin_obj, admin_type = admin_and_type
        admin_id = admin_obj.id

        # Resolve tenant_id
        if admin_type == "system":
            raise HTTPException(400, "System admins must use tenant-specific endpoints")
        effective_tenant_id = acting_tenant_id(request, admin_obj)

        # Check tenant quota
        TenantService.check_quota(db, effective_tenant_id, TenantResourceTypes.END_USERS)

        # Check duplicate username/email
        existing = db.query(EndUser).filter(
            EndUser.tenant_id == effective_tenant_id,
            (EndUser.username == req.username) | (EndUser.email == req.email)
        ).first()
        if existing:
            if existing.username == req.username:
                raise HTTPException(409, f"Username '{req.username}' already exists")
            raise HTTPException(409, f"Email '{req.email}' already exists")

        # Hash password. bcrypt (cost 12) is ~80ms of pure CPU; this is a
        # threadpool handler, so it stays off the event loop.
        password_hash = (
            bcrypt.hashpw(req.password.encode('utf-8'), bcrypt.gensalt())
        ).decode("utf-8")

        # Resolve default role if not specified
        from app.database.models.admin import TenantRole

        role_id = req.role_id
        if role_id and not db.query(TenantRole).filter(
            TenantRole.id == role_id,
            TenantRole.tenant_id == effective_tenant_id,
        ).first():
            raise HTTPException(404, f"Role '{role_id}' not found")
        if not role_id:
            default_role = db.query(TenantRole).filter(
                TenantRole.tenant_id == effective_tenant_id,
                TenantRole.name == "Tenant User"
            ).first()
            if default_role:
                role_id = default_role.id

        # Create user
        user = EndUser(
            tenant_id=effective_tenant_id,
            username=req.username,
            email=req.email,
            display_name=req.display_name or req.username,
            password_hash=password_hash,
            role_id=role_id,
            is_active=True,
            is_verified=True,  # Admin-created users are pre-verified
            avatar_url=req.avatar_url,
        )
        db.add(user)
        db.flush()  # Get the user ID

        # Add to resource groups if specified.
        # First successful group becomes the user's primary group — matches
        # the API-key invariant of "one key -> one group" and provides a
        # clear default runtime scope.
        groups_added = []
        if req.group_ids:
            from app.database.models.resource_group import ResourceGroup
            for gid in req.group_ids:
                group = db.query(ResourceGroup).filter(
                    ResourceGroup.id == gid,
                    ResourceGroup.tenant_id == effective_tenant_id
                ).first()
                if group:
                    # NOTE: `id` is an Integer autoincrement PK (do NOT set it)
                    # and the model has a `role` column, not `membership_type`.
                    # The previous code set both — plus called the nonexistent
                    # `ulid.new()` (ulid-py API; this project uses python-ulid)
                    # — so this path 500'd on every create-with-groups. Matches
                    # the canonical construction in resource_groups.py /
                    # tenant_provisioning.py.
                    membership = ResourceGroupMembership(
                        tenant_id=effective_tenant_id,
                        user_id=user.id,
                        group_id=gid,
                        role="member",
                        added_by=admin_id,
                    )
                    db.add(membership)
                    groups_added.append(gid)
                    if user.primary_group_id is None:
                        user.primary_group_id = gid

        db.commit()

        # Audit log — `UnifiedAdminAuditLogger.log()` is synchronous and
        # takes a different keyword set than the old `log_action` name
        # (which never existed on the class). Extract request metadata
        # manually because `log()` accepts discrete fields, not a
        # Request object.
        audit.log(
            action="create_end_user",
            db=db,
            admin=admin_obj,
            admin_id=admin_id,
            admin_type=admin_type,
            success=True,
            resource_type=TenantResourceTypes.END_USERS.value,
            resource_id=str(user.id),
            tenant_id=effective_tenant_id,
            details={
                "username": req.username,
                "email": req.email,
                "groups_added": groups_added,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        # Send welcome email
        if req.send_welcome_email and req.email:
            try:
                from app.services.email import EmailService
                email_svc = EmailService()
                # Get tenant display name
                from app.database.models.tenant import Tenant
                tenant = db.query(Tenant).filter(Tenant.id == effective_tenant_id).first()
                tenant_name = tenant.display_name if tenant else effective_tenant_id

                # SECURITY: never email the plaintext password. The admin set
                # it and shares it with the user out-of-band; the welcome email
                # only confirms the account (see EmailService.send_welcome_email).
                block_on(email_svc.send_welcome_email(
                    to_email=req.email,
                    username=req.username,
                    display_name=req.display_name or req.username,
                    tenant_name=tenant_name,
                    tenant_id=effective_tenant_id,
                ))
                logger.info(f"Welcome email sent to {req.email}", context={
                    "admin_id": admin_id, "user_id": str(user.id)
                })
            except Exception as e:
                logger.error(f"Failed to send welcome email: {e}", context={
                    "admin_id": admin_id, "user_id": str(user.id)
                })
                # Non-blocking — user is already created

        return {
            "success": True,
            "user": user.to_dict(),
            "groups_added": groups_added,
            "welcome_email_sent": req.send_welcome_email and bool(req.email),
        }

    # ── Get End User Details ──────────────────────────────────────

    @router.get(
        "/{user_id}",
        response_model=UserDetailResponse,
        summary="Get end user details",
        description="Get end user details (tenant isolation enforced)"
    )
    def get_end_user_details(
        user_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_READ))
    ):
        """Get detailed user information including OAuth clients and API keys with tenant verification"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get user info (includes tenant_id check in service)
            user = block_on(end_user_auth.get_gateway_user(user_id=user_id, tenant_id=admin_tenant_id, db=db))
            if not user:
                raise HTTPException(
                    status_code=404,
                    detail=f"User {user_id} not found in tenant {admin_tenant_id}"
                )

            logger.info("End user details accessed", context={
                "admin_id": admin_id,
                "admin_username": admin_username,
                "tenant_id": admin_tenant_id,
                "user_id": user_id
            })

            return UserDetailResponse(
                **user
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get end user details", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to get user details: {str(e)}"
            )

    @router.patch(
        "/{user_id}/status",
        summary="Update end user status",
        description="Activate or deactivate an end user account"
    )
    def update_end_user_status(
        user_id: int,
        status_update: UserStatusUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update end user status with tenant isolation and audit logging"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get user to verify existence and tenant
            user = block_on(end_user_auth.get_gateway_user(user_id, tenant_id=admin_tenant_id, db=db))
            if not user:
                raise HTTPException(
                    status_code=404,
                    detail=f"User {user_id} not found in tenant {admin_tenant_id}"
                )

            # Update status
            updated_user = block_on(end_user_auth.update_gateway_user_status(
                user_id=user_id,
                is_active=status_update.is_active,
                tenant_id=admin_tenant_id,
                db=db
            ))

            # Audit log
            audit.log(
                action="end_user_status_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.END_USERS.value,
                resource_id=str(user_id),
                details={
                    "username": user["username"],
                    "old_status": user["is_active"],
                    "new_status": status_update.is_active
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("End user status updated", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "admin_username": admin_username,
                "tenant_id": admin_tenant_id,
                "user_id": user_id,
                "username": user["username"],
                "old_status": user["is_active"],
                "new_status": status_update.is_active
            })

            return {
                "message": "User status updated successfully",
                "user": updated_user
            }

        except HTTPException:
            raise
        except Exception as e:
            # get_db() handles rollback automatically
            logger.error("Failed to update end user status", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to update user status: {str(e)}"
            )

    @router.patch(
        "/{user_id}/primary-group",
        summary="Set primary resource group for an end user",
        description=(
            "Sets the primary (active) resource group for a portal user. "
            "This group is used as the default runtime scope when the "
            "user's requests do not explicitly carry an X-Resource-Group "
            "header. The user must already be a member of the group."
        ),
    )
    def set_primary_group(
        user_id: int,
        body: "SetPrimaryGroupRequest",
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        from app.database.models.end_user import EndUser
        from app.database.models.resource_group_membership import ResourceGroupMembership

        admin, admin_type = admin_and_type
        admin_tenant_id = acting_tenant_id(request, admin)

        user = db.query(EndUser).filter(
            EndUser.id == user_id,
            EndUser.tenant_id == admin_tenant_id,
        ).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")

        # User must be a current member of the target group.
        membership = db.query(ResourceGroupMembership).filter(
            ResourceGroupMembership.user_id == user_id,
            ResourceGroupMembership.group_id == body.group_id,
            ResourceGroupMembership.tenant_id == admin_tenant_id,
        ).first()
        if not membership:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"User {user_id} is not a member of group {body.group_id}. "
                    "Add the user to the group first."
                ),
            )

        previous = user.primary_group_id
        user.primary_group_id = body.group_id
        db.flush()

        audit.log(
            action="end_user_set_primary_group",
            admin=admin,
            admin_type=admin_type,
            tenant_id=admin_tenant_id,
            success=True,
            resource_type=TenantResourceTypes.END_USERS.value,
            resource_id=str(user_id),
            details={
                "previous_group_id": previous,
                "new_group_id": body.group_id,
            },
            db=db,
        )

        return {
            "user_id": user_id,
            "primary_group_id": body.group_id,
            "previous_primary_group_id": previous,
        }

    @router.delete(
        "/{user_id}",
        summary="Delete end user",
        description="Delete an end user and cleanup associated data (API keys, OAuth tokens)"
    )
    def delete_end_user(
        user_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete end user with tenant isolation, cascade cleanup, and audit logging"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        # Initialize for error handling
        username = None
        user_tenant_id = None

        try:
            # Get user to verify existence
            user = block_on(end_user_auth.get_gateway_user(user_id, tenant_id=admin_tenant_id, db=db))
            if not user:
                raise HTTPException(
                    status_code=404,
                    detail=f"User {user_id} not found in tenant {admin_tenant_id}"
                )

            username = user["username"]
            user_tenant_id = admin_tenant_id

            # Delete user
            block_on(end_user_auth.delete_gateway_user_account(user_id, tenant_id=admin_tenant_id, db=db))

            # Audit successful deletion
            audit.log(
                action="end_user_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=user_tenant_id,
                success=True,
                resource_type=TenantResourceTypes.END_USERS.value,
                resource_id=str(user_id),
                details={
                    "username": username,
                    "cleanup": {
                        "api_keys_deleted": "TBD",
                        "tokens_revoked": "TBD"
                    }
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("End user deleted", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "admin_username": admin_username,
                "tenant_id": user_tenant_id,
                "deleted_user_id": user_id,
                "deleted_username": username
            })

            return {
                "message": "User deleted successfully",
                "user_id": user_id,
                "username": username,
                "deleted_at": datetime.now(timezone.utc).isoformat() + "Z",
                "deleted_by": admin_username
            }

        except HTTPException:
            raise
        except Exception as e:
            # get_db() handles rollback automatically
            logger.error("Failed to delete end user", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete user: {str(e)}"
            )

    @router.get(
        "/export/csv",
        summary="Export end users to CSV",
        description="""
        Export end users to CSV.
        
        Tenant isolation:
        - Tenant admins: Export only their tenant's end users
        - System admins: Export all end users or filter by tenant_id
        """
    )
    def export_end_users_csv(
        request: Request,
        tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_READ))
    ):
        """Export all end users to CSV format with tenant isolation"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get all users
            users, total = block_on(end_user_auth.list_gateway_users(
                tenant_id=admin_tenant_id,
                limit=10000,
                offset=0,
                db=db
            ))

            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow([
                "ID", "Tenant ID", "Username", "Email", "Display Name",
                "Verified", "Active", "Created At", "Last Login"
            ])

            # Write data
            for user in users:
                writer.writerow([
                    user["id"],
                    user.get("tenant_id", ""),
                    user["username"],
                    user.get("email", ""),
                    user.get("display_name", ""),
                    "Yes" if user["is_verified"] else "No",
                    "Yes" if user["is_active"] else "No",
                    user["created_at"],
                    user.get("last_login", "Never")
                ])

            output.seek(0)

            logger.info("End users exported to CSV", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "admin_username": admin_username,
                "tenant_id": admin_tenant_id,
                "user_count": len(users)
            })

            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            tenant_suffix = f"-{admin_tenant_id}" if admin_tenant_id else ""
            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=end-users{tenant_suffix}-{timestamp}.csv"
                    )
                }
            )

        except Exception as e:
            # get_db() handles rollback automatically
            logger.error("Failed to export end users", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to export users: {str(e)}"
            )

    @router.get(
        "/export/json",
        summary="Export end users to JSON",
        description="""
        Export end users to JSON.
        
        Tenant isolation:
        - Tenant admins: Export only their tenant's end users
        - System admins: Export all end users or filter by tenant_id
        """
    )
    def export_end_users_json(
        request: Request,
        tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_READ))
    ):
        """Export all end users to JSON format with tenant isolation"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get all users
            users, total = block_on(end_user_auth.list_gateway_users(
                tenant_id=admin_tenant_id,
                limit=10000,
                offset=0,
                db=db
            ))

            logger.info("End users exported to JSON", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "admin_username": admin_username,
                "tenant_id": admin_tenant_id,
                "user_count": len(users)
            })

            return {
                "users": users,
                "total": len(users),
                "tenant_id": admin_tenant_id,
                "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
                "exported_by": admin_username
            }

        except Exception as e:
            # get_db() handles rollback automatically
            logger.error("Failed to export end users", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to export users: {str(e)}"
            )

    # ── Photo Upload / Serve / Delete ────────────────────────────

    @router.post(
        "/{user_id}/photo",
        summary="Upload end user photo",
        description="Upload a profile photo for an end user (max 5MB, JPEG/PNG/WebP)."
    )
    def upload_end_user_photo(
        user_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        from app.database.models.end_user import EndUser
        import hashlib

        admin_obj, admin_type = admin_and_type
        if admin_type == "system":
            raise HTTPException(400, "Use tenant-specific endpoint")
        tenant_id = acting_tenant_id(request, admin_obj)

        user = db.query(EndUser).filter(
            EndUser.id == user_id, EndUser.tenant_id == tenant_id
        ).first()
        if not user:
            raise HTTPException(404, "User not found")

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
        user.photo_data = data
        user.photo_mime_type = content_type
        user.photo_size = len(data)
        user.photo_hash = photo_hash
        # Embed the FULL hash as the photo capability token (was truncated to
        # 8 hex / 32 bits). The public photo route requires this `?h=` token,
        # so it can't be enumerated by integer id — full length makes the
        # token cryptographically unguessable.
        user.avatar_url = f"/v1/assets/users/end-user/{user_id}/photo?h={photo_hash}"
        db.commit()

        audit.log(
            action="upload_end_user_photo",
            db=db,
            admin=admin_obj,
            admin_id=admin_obj.id,
            admin_type=admin_type,
            success=True,
            resource_type=TenantResourceTypes.END_USERS.value,
            resource_id=str(user.id),
            tenant_id=tenant_id,
            details={
                "username": user.username,
                "content_type": content_type,
                "photo_size": len(data),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return {"success": True, "avatar_url": user.avatar_url}

    @router.delete(
        "/{user_id}/photo",
        summary="Delete end user photo"
    )
    def delete_end_user_photo(
        user_id: int,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.END_USER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        from app.database.models.end_user import EndUser

        admin_obj, admin_type = admin_and_type
        if admin_type == "system":
            raise HTTPException(400, "Use tenant-specific endpoint")
        tenant_id = acting_tenant_id(request, admin_obj)

        user = db.query(EndUser).filter(
            EndUser.id == user_id, EndUser.tenant_id == tenant_id
        ).first()
        if not user:
            raise HTTPException(404, "User not found")

        user.photo_data = None
        user.photo_mime_type = None
        user.photo_size = None
        user.photo_hash = None
        user.avatar_url = None
        db.commit()

        audit.log(
            action="delete_end_user_photo",
            db=db,
            admin=admin_obj,
            admin_id=admin_obj.id,
            admin_type=admin_type,
            success=True,
            resource_type=TenantResourceTypes.END_USERS.value,
            resource_id=str(user.id),
            tenant_id=tenant_id,
            details={"username": user.username},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return {"success": True}

    @router.get(
        "/{user_id}/photo",
        summary="Get end user photo",
        description="Serve the end user's profile photo (public, cacheable)."
    )
    def get_end_user_photo(
        user_id: int,
        request: Request,
        h: Optional[str] = Query(None, description="Photo capability token (prefix of photo_hash)"),
        db: Session = Depends(get_db),
    ):
        """Serve photo, gated by the `?h=` capability token (a prefix of the
        photo_hash) — the canonical route is `/v1/assets/...`; this one
        mirrors it. The token stops enumeration of photos by integer id and
        cross-tenant scraping; 404 when absent/mismatched."""
        from app.database.models.end_user import EndUser
        from app.core.photo_token import photo_token_ok
        from fastapi.responses import Response

        user = db.query(EndUser).filter(EndUser.id == user_id).first()
        if not user or not user.photo_data or not photo_token_ok(user.photo_hash, h):
            raise HTTPException(404, "Photo not found")

        # ETag conditional request
        etag = user.photo_hash or ""
        if_none_match = request.headers.get("if-none-match", "")
        if etag and if_none_match == etag:
            return Response(status_code=304)

        return Response(
            content=user.photo_data,
            media_type=user.photo_mime_type or "image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "ETag": etag,
            },
        )

    return router