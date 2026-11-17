# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Authentication endpoints Pydantic REQUEST/RESPONSE models
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict

from pydantic import ConfigDict

from app.constants.tenant import TenantDefault
from app.core.password_rule import NewPassword

class SystemAdminLoginRequest(BaseModel):
    """Login request for system administrators"""
    username: str = Field(..., min_length=3, max_length=100, description="System admin username")
    password: str = Field(..., min_length=1, description="System admin password")

    class Config:
        json_schema_extra = {
            "example": {
                "username": "sysadmin",
                "password": "<the administrator's password>"
            }
        }


class TenantAdminLoginRequest(BaseModel):
    """Login request for tenant administrators"""
    tenant_id: Optional[str] = Field(None, description="Tenant ID (optional: resolved from the user name when omitted)")
    username: str = Field(..., min_length=3, max_length=100, description="Tenant admin username")
    password: str = Field(..., min_length=1, description="Tenant admin password")

    class Config:
        json_schema_extra = {
            "example": {
                "tenant_id": TenantDefault.ID.value,
                "username": "admin",
                "password": "<the administrator's password>"
            }
        }

class LoginResponse(BaseModel):
    """
    Unified login response for both system and tenant admins
    """
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type (always 'bearer')")
    expires_in: int = Field(..., description="Token expiration time in seconds")
    admin: dict = Field(..., description="Admin user information")
    session_id: Optional[str] = Field(None, description="Session identifier")

    class Config:
        json_schema_extra = {
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "expires_in": 3600,
                "admin": {
                    "id": "admin-123",
                    "username": "sysadmin",
                    "email": "sysadmin@system.local",
                    "full_name": "System Administrator",
                    "role_id": "role-tenant-admin",
                    "role_name": "Tenant Admin",
                    "permissions": ["system:admin"],
                    "admin_type": "system",
                    "tenant_id": None,
                    "is_active": True,
                    "last_login": "2025-11-02T10:30:00Z"
                },
                "session_id": "session-abc-123"
            }
        }

class LogoutResponse(BaseModel):
    """Logout response"""
    message: str = Field(default="Successfully logged out", description="Logout confirmation message")

    class Config:
        json_schema_extra = {
            "example": {
                "message": "Successfully logged out"
            }
        }

class CreateSystemAdminRequest(BaseModel):
    """Create system admin request"""
    username: str = Field(..., min_length=3, max_length=100, description="Username for the new system admin")
    email: EmailStr = Field(..., description="Email address")
    password: NewPassword
    full_name: Optional[str] = Field(None, description="Full name of the admin")
    roles: List[str] = Field(default_factory=list, description="List of role names to assign")

    class Config:
        json_schema_extra = {
            "example": {
                "username": "newsysadmin",
                "email": "newsysadmin@system.local",
                "password": "SecurePass123!",
                "full_name": "New System Admin",
                "roles": ["super_admin"]
            }
        }


class CreateTenantAdminRequest(BaseModel):
    """Create tenant admin request"""
    tenant_id: str = Field(..., description="Tenant ID this admin belongs to")
    username: str = Field(..., min_length=3, max_length=100, description="Username for the new tenant admin")
    email: EmailStr = Field(..., description="Email address")
    password: NewPassword
    full_name: Optional[str] = Field(None, description="Full name of the admin")
    role_id: Optional[str] = Field(None, description="Role ID to assign (defaults to 'Tenant Admin' role)")

    class Config:
        json_schema_extra = {
            "example": {
                "tenant_id": "acme-corp",
                "username": "newadmin",
                "email": "admin@acme-corp.com",
                "password": "SecurePass123!",
                "full_name": "New Tenant Admin",
                "role_id": "role-tenant-admin"
            }
        }

class AdminResponse(BaseModel):
    """Admin user response (for creation, updates, etc.)"""
    id: str = Field(..., description="Admin user ID")
    username: str = Field(..., description="Username")
    email: str = Field(..., description="Email address")
    full_name: Optional[str] = Field(None, description="Full name")
    admin_type: str = Field(..., description="Admin type: 'system' or 'tenant'")
    tenant_id: Optional[str] = Field(None, description="Tenant ID (only for tenant admins)")
    role_id: Optional[str] = Field(None, description="Role ID (for tenant admins)")
    role_name: Optional[str] = Field(None, description="Role name (for tenant admins)")
    permissions: List[str] = Field(..., description="List of permissions")
    is_active: bool = Field(..., description="Whether the admin is active")
    is_locked: bool = Field(..., description="Whether the admin account is locked")
    last_login: Optional[str] = Field(None, description="Last login timestamp (ISO format)")
    created_at: str = Field(..., description="Account creation timestamp (ISO format)")
    password_change_required: bool = Field(
        False,
        description=(
            "The account's password was generated for it (the first administrator of a tenant "
            "created without credentials) and must be changed, with "
            "`POST /v1/admin/users/me/change-password`, before any other admin route answers it."
        ),
    )

    class Config:
        from_attributes = True  # Pydantic V2
        json_schema_extra = {
            "example": {
                "id": "admin-123",
                "username": "admin",
                "email": "admin@example.com",
                "full_name": "Administrator",
                "admin_type": "tenant",
                "tenant_id": TenantDefault.ID.value,
                "role_id": "role-tenant-admin",
                "role_name": "Tenant Admin",
                "permissions": ["mcp:server:read", "mcp:server:create"],
                "is_active": True,
                "is_locked": False,
                "last_login": "2025-11-02T10:30:00Z",
                "created_at": "2025-11-01T08:00:00Z"
            }
        }


class CurrentAdminResponse(AdminResponse):
    """The administrator who is asking, and what it is in this installation."""

    installation_has_system_admin: bool = Field(
        ...,
        description=(
            "Whether the installation has a system administrator: an administrator above "
            "the tenants, who manages them and operates what they share."
        ),
    )
    is_installation_operator: bool = Field(
        ...,
        description=(
            "Whether the caller may operate what the whole installation shares, such as "
            "pausing, resuming and triggering a background scheduler: the system "
            "administrator where the installation has one, the tenant's administrator "
            "where it has none. Those routes also ask for their permission."
        ),
    )


class TokenValidationResponse(BaseModel):
    """Token validation response"""
    valid: bool = Field(..., description="Whether the token is valid")
    admin: Optional[dict] = Field(None, description="Admin information if token is valid")

    class Config:
        json_schema_extra = {
            "example": {
                "valid": True,
                "admin": {
                    "id": "admin-123",
                    "username": "sysadmin",
                    "email": "sysadmin@system.local",
                    "admin_type": "system",
                    "role_id": "role-system-admin",
                    "role_name": "System Admin"
                }
            }
        }


# =============================================================================
# ROLE MANAGEMENT MODELS
# =============================================================================

class PermissionInfo(BaseModel):
    """Information about a single permission"""
    name: str = Field(..., description="Permission string (e.g., 'mcp:server:read')")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Description of what this permission grants")
    category: str = Field(..., description="Permission category (e.g., 'mcp', 'admin')")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "mcp:server:read",
                "display_name": "MCP Server Read",
                "description": "View MCP server configurations",
                "category": "mcp"
            }
        }


class PermissionsListResponse(BaseModel):
    """Response with all available permissions organized by category"""
    permissions_by_category: Dict[str, List[PermissionInfo]] = Field(
        ...,
        description="Permissions organized by category"
    )
    total_permissions: int = Field(..., description="Total number of permissions")

    class Config:
        json_schema_extra = {
            "example": {
                "permissions_by_category": {
                    "mcp": [
                        {
                            "name": "mcp:server:read",
                            "display_name": "MCP Server Read",
                            "description": "View MCP server configurations",
                            "category": "mcp"
                        }
                    ],
                    "admin": [
                        {
                            "name": "admin:user:read",
                            "display_name": "Admin User Read",
                            "description": "View admin user information",
                            "category": "admin"
                        }
                    ]
                },
                "total_permissions": 85
            }
        }


class RoleResponse(BaseModel):
    """Response model for a single role"""
    id: str = Field(..., description="Role ID")
    tenant_id: str = Field(..., description="Tenant ID this role belongs to")
    name: str = Field(..., description="Role name")
    description: Optional[str] = Field(None, description="Role description")
    is_system: bool = Field(..., description="Whether this is a system-managed role")
    permissions: List[str] = Field(..., description="List of permission strings")
    permission_count: int = Field(..., description="Number of permissions")
    created_at: str = Field(..., description="Creation timestamp (ISO format)")
    updated_at: Optional[str] = Field(None, description="Last update timestamp (ISO format)")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "role-tenant-admin",
                "tenant_id": "default",
                "name": "Tenant Admin",
                "description": "Full administrative access within tenant",
                "is_system": True,
                "permissions": [
                    "mcp:server:read",
                    "mcp:server:create",
                    "admin:user:read"
                ],
                "permission_count": 45,
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z"
            }
        }


class RoleListResponse(BaseModel):
    """Response model for listing roles"""
    roles: List[RoleResponse] = Field(..., description="List of roles")
    total: int = Field(..., description="Total number of roles")
    system_roles: int = Field(..., description="Number of system roles")
    custom_roles: int = Field(..., description="Number of custom roles")

    class Config:
        json_schema_extra = {
            "example": {
                "roles": [
                    {
                        "id": "role-tenant-admin",
                        "tenant_id": "default",
                        "name": "Tenant Admin",
                        "description": "Full administrative access within tenant",
                        "is_system": True,
                        "permissions": ["mcp:server:read", "mcp:server:create"],
                        "permission_count": 45,
                        "created_at": "2025-01-01T00:00:00Z"
                    }
                ],
                "total": 3,
                "system_roles": 3,
                "custom_roles": 0
            }
        }


class UpdateTenantAdminRequest(BaseModel):
    """What `PATCH /v1/admin/users/tenant/{admin_id}` changes. A field that
    is not sent is left as it is; a field that is not listed is refused.
    The role is changed on `PATCH /v1/admin/users/tenant/{admin_id}/role`."""

    model_config = ConfigDict(extra="forbid")

    email: Optional[EmailStr] = Field(None, description="Email address")
    full_name: Optional[str] = Field(None, description="Full name of the admin")
    is_active: Optional[bool] = Field(None, description="False deactivates the account")
    password: Optional[NewPassword] = Field(
        None,
        description=(
            "A new password for another administrator (at least 8 characters, not blank); "
            "their sessions end. An administrator changes its own on "
            "`POST /v1/admin/users/me/change-password`."
        ),
    )


class CreateRoleRequest(BaseModel):
    """Request model for creating a new role"""
    name: str = Field(..., min_length=1, max_length=100, description="Role name")
    description: Optional[str] = Field(None, max_length=500, description="Role description")
    permissions: List[str] = Field(..., description="List of permission strings to assign")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Developer",
                "description": "Developers with read/write access to MCP servers",
                "permissions": [
                    "mcp:server:read",
                    "mcp:server:create",
                    "mcp:server:update",
                    "mcp:server:test"
                ]
            }
        }


class UpdateRoleRequest(BaseModel):
    """Request model for updating a role"""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New role name")
    description: Optional[str] = Field(None, max_length=500, description="New role description")
    permissions: Optional[List[str]] = Field(None, description="New list of permissions")

    class Config:
        extra = "forbid"
        json_schema_extra = {
            "example": {
                "name": "Senior Developer",
                "description": "Senior developers with extended access",
                "permissions": [
                    "mcp:server:read",
                    "mcp:server:create",
                    "mcp:server:update",
                    "mcp:server:delete",
                    "mcp:server:test"
                ]
            }
        }


class AssignRoleRequest(BaseModel):
    """Request model for assigning a role to an admin"""
    role_id: str = Field(..., description="ID of the role to assign")

    class Config:
        extra = "forbid"
        json_schema_extra = {
            "example": {
                "role_id": "role-tenant-user"
            }
        }