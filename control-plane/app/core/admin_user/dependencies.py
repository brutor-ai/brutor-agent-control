# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
FastAPI Dependencies for Multi-Tenant Admin RBAC
Supports both SystemAdmin (platform-wide) and TenantAdmin (tenant-scoped) users
"""

from typing import Optional, List, Callable, Union
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.env_vars import JWT_SECRET
from app.constants.admin import AdminType
from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.resource_mixins import set_current_actor
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.tenant import Tenant
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.composition.principals import PRINCIPALS
from app.core.jwt_settings import jwt_algorithm
from app.middleware.trusted_proxies import client_ip

# Security scheme
security = HTTPBearer(auto_error=False)

# Global service instances
_auth_service: Optional[UnifiedAuthService] = None
_audit_logger: Optional[UnifiedAdminAuditLogger] = None


def get_auth_service() -> UnifiedAuthService:
    """Get or create unified authentication service"""
    global _auth_service

    if not _auth_service:
        jwt_secret = JWT_SECRET.exact()
        if not jwt_secret:
            raise ValueError(JWT_SECRET.name + " environment variable is required")
        # The same default as the token issuer's, read in one place.
        _auth_service = UnifiedAuthService(jwt_secret=jwt_secret, jwt_algorithm=jwt_algorithm())

    return _auth_service


def get_audit_logger() -> UnifiedAdminAuditLogger:
    """Get or create unified audit logger"""
    global _audit_logger

    if not _audit_logger:
        _audit_logger = UnifiedAdminAuditLogger()

    return _audit_logger


# =============================================================================
# UNIFIED AUTHENTICATION - Works for both System and Tenant Admins
# =============================================================================

#: What an administrator whose password was generated for it may do before
#: it has changed that password: sign out, read who it is, change it.
PASSWORD_CHANGE_ROUTES = frozenset({
    ("POST", "/admin/users/me/change-password"),
    ("POST", "/admin/users/logout"),
    ("GET", "/admin/users/me"),
})


def refuse_until_password_changed(request: Request, admin) -> None:
    """403 `password_change_required` for an account that must change its
    generated password first, on every route but `PASSWORD_CHANGE_ROUTES`."""
    if not getattr(admin, "password_change_required", False):
        return
    path = request.url.path
    if path.startswith("/v1/"):
        path = path[len("/v1"):]
    if (request.method, path) in PASSWORD_CHANGE_ROUTES:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "password_change_required",
            "message": (
                "This account's password was generated for it and must be changed first: "
                "POST /v1/admin/users/me/change-password."
            ),
        },
    )


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    auth_service: UnifiedAuthService = Depends(get_auth_service),
    db: Session = Depends(get_db)
) -> tuple[AdminPrincipal, AdminType]:
    """
    Get currently authenticated admin (system or tenant)

    Returns:
        Tuple of (Admin, admin_type) where admin_type is AdminType enum
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication token required")

    claims = auth_service.token_claims(credentials.credentials)
    result = auth_service.verify_claims(claims, db) if claims else None

    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    admin, session, admin_type = result

    refuse_until_password_changed(request, admin)

    # Store admin info in request.state for easy access
    request.state.admin = admin
    request.state.admin_type = admin_type
    # `session` is None for a session verified a moment ago (the verifier
    # does not look it up again); its id is the token's `jti` either way.
    request.state.admin_session = session
    request.state.admin_session_id = getattr(session, "id", None) or claims.get("jti")

    # For tenant admins, also store tenant_id
    if admin_type == AdminType.TENANT_ADMIN_TYPE:
        request.state.tenant_id = admin.tenant_id

    # Bind the acting admin for AI Resource authorship stamping.
    #
    # Every admin write path in the control plane already depends on this
    # function, so binding here means `created_by` / `updated_by` cannot be
    # forgotten by an endpoint author — and, more importantly, cannot be
    # supplied by the caller. A field recording *who did this* that the
    # client can set records nothing.
    #
    # Not reset afterwards: ContextVars set inside a request are scoped to
    # that request's context by the ASGI server, and FastAPI runs each
    # request in its own. A reset here would need a dependency teardown
    # that fires after the response, which is exactly when the flush we
    # care about has already happened.
    set_current_actor(getattr(admin, "username", None))

    return admin, admin_type


# =============================================================================
# TENANT SCOPING - which tenant an admin request acts on
# =============================================================================

def resolve_tenant_id(
    admin,
    admin_type: str,
    tenant_id_query: Optional[str] = None,
) -> str:
    """Decide which tenant this request acts on.

    The principal decides (`app/composition/principals.py`): a
    tenant admin is pinned to its own tenant, and naming another is refused
    with 403, never silently replaced by its own; a system admin has no
    tenant of its own and must name one (400).

    This lived as a five-line copy in twenty-five endpoint modules, which
    is how it came to disagree with itself: most copies *silently ignored*
    a mismatched `?tenant_id=`, while `compliance.py` and
    `data_erasure.py` refused it with 403. Then it lived here as "a tenant
    admin, or else the system admin's branch", so a type nobody knew was
    handed the tenant it asked for. A type that is not a registered
    principal acts on no tenant: 403.

    `admin_type` accepts the plain strings and `AdminType` members alike.
    """
    principal = PRINCIPALS.get(admin_type)
    if principal is None:
        raise HTTPException(
            status_code=403,
            detail="Unknown administrator type",
        )
    return principal.resolve_tenant(admin, tenant_id_query)


def acting_tenant_id(request: Request, admin) -> str:
    """The tenant a request acts on, for a route that takes it from whoever
    called and declares no `tenant_id` of its own.

    These routes read `admin.tenant_id`. A system administrator's row has no
    such attribute, so each of them (a hundred and some: resource groups, end
    users, proxy logs, approvals, the model runner, batch jobs) raised
    `AttributeError` and answered 500 to it, whether it named a tenant or not.
    Here the administrator's own tenant is the answer, as it always
    was; an administrator without one is asked of its principal, which takes
    the tenant the request names in `?tenant_id=` — where
    `require_tenant_resource_access` reads it — or refuses: 400 "System
    admins must specify tenant_id" for the system administrator. An object no
    registered principal owns acts on no tenant: 403.
    """
    own = getattr(admin, "tenant_id", None)
    if own:
        return own
    principal = PRINCIPALS.owner_of(admin)
    if principal is None:
        raise HTTPException(
            status_code=403,
            detail="Unknown administrator type",
        )
    return principal.resolve_tenant(admin, request.query_params.get("tenant_id"))


def tenant_scope(admin, admin_type, tenant_id_query: Optional[str] = None) -> Optional[str]:
    """The tenant a listing is limited to, or None for "every tenant".

    For the routes that list across tenants for the system administrator
    (administrators, their sessions, the audit trail): a platform-wide
    principal gets the tenant it names, or None when it names none; any other
    registered principal gets what `resolve_tenant_id` gives it (a tenant
    administrator its own tenant, 403 for naming another). A type that is not
    a registered principal is refused with 403: None is never its answer.

    These routes used to read "a tenant administrator, or else every tenant".
    """
    if is_platform_wide(admin_type):
        return tenant_id_query or None
    return resolve_tenant_id(admin, admin_type, tenant_id_query)


def may_act_on_tenant(admin, admin_type, tenant_id: Optional[str]) -> bool:
    """May the caller act on something that belongs to `tenant_id`?

    For the routes that load an object first and then ask whose it is. A
    platform-wide principal may; any other registered principal only where
    `tenant_id` is the tenant it is pinned to; a type that is not a
    registered principal never. These routes used to read "refuse a tenant
    administrator of another tenant", which let everything else through.
    """
    if is_platform_wide(admin_type):
        return True
    principal = PRINCIPALS.get(admin_type)
    if principal is None or not tenant_id:
        return False
    try:
        return principal.resolve_tenant(admin, None) == tenant_id
    except HTTPException:
        return False


def is_platform_wide(admin_type) -> bool:
    """Is the caller a platform-wide principal (the system administrator)?

    False for a tenant administrator and for a type nobody registered. The
    question route modules used to ask as `admin_type == SYSTEM_ADMIN_TYPE`.
    """
    return PRINCIPALS.is_platform_wide(admin_type)


def is_platform_admin(admin, admin_type) -> bool:
    """May the caller administer what belongs to the installation (the global
    catalogs)? The system administrator where one exists; in a single-tenant
    installation, the tenant administrator holding `platform:catalog:manage`.
    See `app/composition/principals.py`."""
    return PRINCIPALS.is_platform_admin(admin_type, admin)


# =============================================================================
# SYSTEM ADMIN ONLY - Platform-wide access
# =============================================================================

def require_system_admin(
    request: Request,
    admin_and_type: tuple = Depends(get_current_admin)
) -> AdminPrincipal:
    """
    Require system admin authentication

    System admins have platform-wide access and can:
    - Manage all tenants
    - View cross-tenant data
    - Configure platform settings

    Asked of the principal registry: a platform-wide principal, registered.
    Where none is registered (the Community composition) nobody passes.
    """
    admin, admin_type = admin_and_type

    if not is_platform_wide(admin_type):
        raise HTTPException(
            status_code=403,
            detail="System administrator access required"
        )

    return admin


# =============================================================================
# INSTALLATION OPERATOR - runtime operations on what every tenant shares
# =============================================================================

def is_installation_operator(admin_type) -> bool:
    """Whether a principal of this type operates what the whole installation
    shares: the system administrator where one exists, the tenant's
    administrator in an installation that has none."""
    if PRINCIPALS.get(admin_type) is None:
        return False
    return is_platform_wide(admin_type) or not PRINCIPALS.has_platform_wide()


def require_installation_operator(
    admin_and_type: tuple = Depends(get_current_admin)
) -> AdminPrincipal:
    """
    Require the caller who may operate what the whole installation shares
    (a background scheduler runs once for every tenant).

    - where a system administrator exists: the system administrator, and no
      tenant administrator whatever its role holds;
    - in a single-tenant installation (no system administrator): the
      tenant's administrator. The one tenant is the installation.

    The route still asks for its permission (`require_permission`).
    """
    admin, admin_type = admin_and_type

    if not is_installation_operator(admin_type):
        raise HTTPException(
            status_code=403,
            detail="System administrator access required: this acts on the whole installation"
        )

    return admin


# =============================================================================
# PLATFORM ADMIN - what belongs to the installation, not to a tenant
# =============================================================================

def require_platform_admin(
    admin_and_type: tuple = Depends(get_current_admin)
) -> AdminPrincipal:
    """
    Require the administrator of the installation.

    Guards the writes of what every tenant shares: the global model catalog
    and the global MCP catalog. Who that is depends on the principals the
    installation has:

    - where a system administrator exists: the system administrator, and
      no tenant administrator whatever its role holds;
    - in a single-tenant installation (no system administrator): the tenant
      administrator holding `platform:catalog:manage`.

    Anyone else, and a type nobody registered, gets 403.
    """
    admin, admin_type = admin_and_type

    if not is_platform_admin(admin, admin_type):
        raise HTTPException(
            status_code=403,
            detail="Platform administrator access required"
        )

    return admin


# =============================================================================
# TENANT ADMIN ONLY - Tenant-scoped access
# =============================================================================

def get_current_tenant_id(request: Request) -> str:
    """
    Get current tenant ID from authenticated tenant admin

    This is automatically set by tenant admin authentication.
    For system admins accessing tenant resources, they must specify tenant_id explicitly.
    """
    if not hasattr(request.state, 'tenant_id'):
        raise HTTPException(
            status_code=500,
            detail="Tenant context not initialized"
        )

    return request.state.tenant_id


async def get_current_tenant(request: Request, db: Session = Depends(get_db)) -> Tenant:
    """
    Get current tenant object

    Works for tenant admins (uses their tenant_id)
    """
    tenant_id = get_current_tenant_id(request)

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()

    if not tenant:
        raise HTTPException(
            status_code=404,
            detail=f"Tenant '{tenant_id}' not found"
        )

    return tenant


# =============================================================================
# ADMIN PERMISSION-BASED ACCESS CONTROL
# =============================================================================

def require_permission(permission):
    """
    Decorator factory for admin permission-based access control
    Works for both system and tenant admins

    Args:
        permission: Either a Permission enum member or a permission string

    Returns:
        Dependency function
    """
    def permission_dependency(
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ) -> AdminPrincipal:

        admin, admin_type = admin_and_type

        # Handle both Permission enum and string values
        permission_str = permission.value if hasattr(permission, 'value') else str(permission)

        # Check if admin has the required permission
        if not admin.has_permission(permission_str):
            audit.log(
                action=f"unauthorized_access_{permission_str}",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin.tenant_id if admin_type == AdminType.TENANT_ADMIN_TYPE else None,
                success=False,
                resource_type="permission",
                resource_id=permission_str,
                details={
                    "admin_type": admin_type,
                    "required_permission": permission_str
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=403,
                detail=f"Permission required: {permission_str}"
            )

        return admin

    # What the route requires, readable from the route (its dependant tree).
    permission_dependency.required_permissions = (  # type: ignore[attr-defined]
        permission.value if hasattr(permission, "value") else str(permission),
    )
    return permission_dependency


def require_permissions(permissions: List[str]) -> Callable:
    """
    Require multiple admin permissions (admin must have at least one)

    Args:
        permissions: List of permission strings

    Returns:
        Dependency function
    """
    def dependency(
        request: Request,
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ) -> AdminPrincipal:

        admin, admin_type = admin_and_type

        # Check if admin has any of the required permissions
        has_permission = any(
            admin.has_permission(perm) for perm in permissions
        )

        if not has_permission:
            audit.log(
                action=f"unauthorized_access_multiple",
                admin=admin,
                admin_type=admin_type,
                tenant_id=admin.tenant_id if admin_type == AdminType.TENANT_ADMIN_TYPE else None,
                success=False,
                resource_type=TenantResourceTypes.PERMISSIONS.value,
                resource_id=",".join(permissions),
                details={
                    "admin_type": admin_type,
                    "required_permissions": permissions
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=403,
                detail=f"One of these permissions required: {', '.join(permissions)}"
            )

        return admin

    dependency.required_permissions = tuple(permissions)  # type: ignore[attr-defined]
    return dependency


# =============================================================================
# TENANT-SCOPED RESOURCE ACCESS
# =============================================================================

def require_tenant_resource_access(
    resource_type: TenantResourceTypes,
    allow_system_admin: bool = True
):
    """
    Ensure admin has access to tenant-scoped resources

    - Tenant admins: Can only access their own tenant's resources
    - System admins: Can access any tenant's resources (if allow_system_admin=True)

    Args:
        resource_type: Type of resource being accessed
        allow_system_admin: Whether system admins can access this resource
    """

    def dependency(
            request: Request,
            admin_and_type: tuple = Depends(get_current_admin),
            db: Session = Depends(get_db)
    ) -> tuple[AdminPrincipal, str, AdminType]:

        admin, admin_type = admin_and_type

        if is_platform_wide(admin_type):
            if not allow_system_admin:
                raise HTTPException(
                    status_code=403,
                    detail="System admins cannot access this resource"
                )

            # System admin must specify tenant_id in query params or path
            tenant_id = request.query_params.get('tenant_id') or request.path_params.get('tenant_id')

            if not tenant_id:
                raise HTTPException(
                    status_code=400,
                    detail="System admins must specify tenant_id"
                )

            # Validate tenant exists
            tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
            if not tenant:
                raise HTTPException(
                    status_code=404,
                    detail=f"Tenant '{tenant_id}' not found"
                )

            # Set tenant context for system admin
            request.state.tenant_id = tenant_id

            return admin, tenant_id, admin_type

        elif admin_type == AdminType.TENANT_ADMIN_TYPE:
            # Tenant admin automatically uses their tenant_id
            tenant_id = admin.tenant_id
            request.state.tenant_id = tenant_id

            return admin, tenant_id, admin_type

        else:
            # A type that is not a registered principal reaches no tenant.
            raise HTTPException(
                status_code=403,
                detail="Unknown administrator type"
            )

    return dependency


async def verify_tenant_owns_resource(
    resource,
    request: Request,
    resource_type: TenantResourceTypes
) -> None:
    """
    Verify that a resource belongs to the current admin's tenant

    - For tenant admins: Ensures resource.tenant_id matches admin.tenant_id
    - For system admins: Always passes (they can access any tenant)

    Args:
        resource: SQLAlchemy model instance with tenant_id
        request: FastAPI request
        TenantResourceTypes: Resource type for error message

    Raises:
        HTTPException: If resource doesn't belong to tenant admin's tenant
    """
    if not resource:
        raise HTTPException(
            status_code=404,
            detail=f"{resource_type} not found"
        )

    # Get admin type
    admin_type = getattr(request.state, 'admin_type', None)

    # System admins can access any tenant's resources
    if is_platform_wide(admin_type):
        return

    # Tenant admins can only access their tenant's resources
    if not hasattr(resource, 'tenant_id'):
        return  # Resource doesn't have tenant_id

    admin_tenant_id = getattr(request.state, 'tenant_id', None)

    if not admin_tenant_id:
        raise HTTPException(
            status_code=500,
            detail="Tenant context not initialized"
        )

    if resource.tenant_id != admin_tenant_id:
        raise HTTPException(
            status_code=404,
            detail=f"{resource_type} not found"  # Don't leak that it exists in another tenant
        )
