# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin User RBAC Database Models - CLEAN VERSION
Models for system admins, tenant admins, roles, sessions, and audit logs

SECURITY MODEL:
- SystemAdmin: Platform-level administrators (no tenant_id)
- TenantAdmin: Tenant-scoped administrators (required tenant_id)
- TenantRole: Tenant-scoped roles with customizable permissions
- Separate tables prevent privilege escalation attacks

This is the CLEAN version for fresh installations - no legacy JSON fields
"""

from datetime import datetime, timezone
from typing import Dict, Any, List

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text, JSON, Integer, Float, Index, UniqueConstraint, LargeBinary, text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

import bcrypt

from app.database import Base
from app.core.admin_user import lockout


class SystemAdmin(Base):
    """
    System administrators with platform-wide access

    These admins can:
    - Create and manage tenants
    - View all tenant data
    - Configure platform settings
    - Manage other system admins

    Security: NO tenant_id - exists outside tenant hierarchy
    """
    __tablename__ = "system_admins"

    id = Column(String(50), primary_key=True)

    # NO tenant_id - system admins are global
    username = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=True)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)
    is_locked = Column(Boolean, default=False, nullable=False)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    # When a lock set by failed sign-ins ends (`app/core/admin_user/lockout.py`).
    # NULL with `is_locked`: a lock without an end.
    locked_until = Column(DateTime(timezone=True), nullable=True)

    # Permissions (JSON for system admins - they don't use TenantRole)
    # System admins typically just have ["system:admin"] permission
    permissions = Column(JSON, nullable=False, default=lambda: [])

    # Audit fields
    last_login = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = Column(String(50), nullable=True, comment="System admin who created this account")

    # 2FA / TOTP — mirrors the TenantAdmin fields below so the same
    # /admin/users/me/2fa/* endpoints work for both admin types.
    # Nullable: 2FA is opt-in; an admin that hasn't enrolled has
    # NULL for secret/verified_at/backup_codes and enabled=false.
    totp_secret = Column(Text, nullable=True, comment="Encrypted TOTP shared secret. Null until /2fa/setup runs.")
    totp_enabled = Column(Boolean, default=False, nullable=False, comment="True once a verification code has been accepted.", server_default=text("false"))
    totp_verified_at = Column(DateTime(timezone=True), nullable=True, comment="When 2FA was first verified.")
    backup_codes = Column(JSON, nullable=True, comment="Hashed one-time backup codes. Each successful use pops one.")

    # Relationships
    sessions = relationship("SystemAdminSession", back_populates="admin", cascade="all, delete-orphan")
    audit_logs = relationship("SystemAdminAuditLog", back_populates="admin")

    __table_args__ = (
        UniqueConstraint('username', name='uix_system_admins_username'),
        UniqueConstraint('email', name='uix_system_admins_email'),
        Index('ix_system_admins_active', 'is_active'),
        Index('ix_system_admins_locked', 'is_locked'),
        Index('ix_system_admins_username', 'username'),
        Index('ix_system_admins_email', 'email'),
        Index('ix_system_admins_created', 'created_at'),
        {'comment': 'Platform-level system administrators (no tenant association)'}
    )

    def __repr__(self) -> str:
        return f"<SystemAdmin(id='{self.id}', username='{self.username}')>"

    def has_permission(self, permission_value: str) -> bool:
        """Check if system admin has specific permission"""
        return permission_value in self.permissions or "system:admin" in self.permissions

    def set_password(self, password: str) -> None:
        """Hash and set password for sys admin"""
        from app.core.password_rule import check_password

        check_password(password)
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
        self.password_hash = password_hash
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "admin_type": "system",
            "username": self.username,
            "email": self.email,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "is_locked": lockout.currently_locked(self),
            "roles": ["super_admin"] if "system:admin" in self.permissions else [],
            "permissions": self.permissions or [],
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "totp_enabled": self.totp_enabled or False,
        }

        if include_sensitive:
            data["failed_login_attempts"] = self.failed_login_attempts

        return data


class TenantRole(Base):
    """
    Tenant-scoped roles with customizable permissions

    Each tenant can define their own roles with custom permission sets.
    System roles (is_system=True) are created during tenant initialization.
    """
    __tablename__ = "tenant_roles"

    id = Column(String(50), primary_key=True)

    # Multi-tenancy - REQUIRED
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this role belongs to"
    )

    name = Column(String(100), nullable=False, comment="Role name (unique within tenant)")
    description = Column(Text, nullable=True, comment="Human-readable description of the role")

    # System roles can be modified but not deleted
    is_system = Column(Boolean, default=False, nullable=False, comment="System role (cannot be deleted)")

    # Audit fields
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = Column(String(50), nullable=True, comment="Admin who created this role")

    # Relationships
    permissions = relationship("TenantRolePermission", back_populates="role", cascade="all, delete-orphan")
    tenant_admins = relationship("TenantAdmin", back_populates="role")

    __table_args__ = (
        UniqueConstraint('tenant_id', 'name', name='uix_tenant_roles_tenant_name'),
        Index('ix_tenant_roles_tenant', 'tenant_id'),
        Index('ix_tenant_roles_tenant_system', 'tenant_id', 'is_system'),
        Index('ix_tenant_roles_tenant_name', 'tenant_id', 'name'),
        {'comment': 'Tenant-scoped roles with customizable permissions'}
    )

    def __repr__(self) -> str:
        return f"<TenantRole(id='{self.id}', tenant_id='{self.tenant_id}', name='{self.name}', is_system={self.is_system})>"

    def get_permissions(self) -> List[str]:
        """Get list of permission strings for this role"""
        return [rp.permission for rp in self.permissions]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "is_system": self.is_system,
            "permissions": self.get_permissions(),
            "permission_count": len(self.permissions),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_by": self.created_by
        }


class TenantRolePermission(Base):
    """
    Junction table linking roles to permissions

    This allows flexible permission assignment to roles.
    """
    __tablename__ = "tenant_role_permissions"

    id = Column(String(50), primary_key=True)

    role_id = Column(
        String(50),
        ForeignKey("tenant_roles.id", ondelete="CASCADE"),
        nullable=False,
        comment="Role this permission belongs to"
    )

    permission = Column(String(100), nullable=False, comment="Permission string (e.g., 'mcp:server:create')")

    # Audit
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    role = relationship("TenantRole", back_populates="permissions")

    __table_args__ = (
        UniqueConstraint('role_id', 'permission', name='uix_tenant_role_permissions_role_permission'),
        Index('ix_tenant_role_permissions_role', 'role_id'),
        Index('ix_tenant_role_permissions_permission', 'permission'),
        {'comment': 'Permissions assigned to tenant roles'}
    )

    def __repr__(self) -> str:
        return f"<TenantRolePermission(role_id='{self.role_id}', permission='{self.permission}')>"


class TenantAdmin(Base):
    """
    Tenant-scoped administrators with role-based access control

    These admins can:
    - Manage their tenant's users, servers, and configurations
    - View only their tenant's data
    - Cannot see or access other tenants

    Security: REQUIRED tenant_id - scoped to single tenant
    """
    __tablename__ = "tenant_admins"

    id = Column(String(50), primary_key=True)

    # Multi-tenancy - REQUIRED (not nullable)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this admin belongs to"
    )

    # Role assignment - links to TenantRole (REQUIRED for new installs)
    role_id = Column(
        String(50),
        ForeignKey("tenant_roles.id", ondelete="SET NULL"),
        nullable=False,  # Required for fresh installs
        comment="Role assigned to this admin"
    )

    username = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=True)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)
    is_locked = Column(Boolean, default=False, nullable=False)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    # When a lock set by failed sign-ins ends (`app/core/admin_user/lockout.py`).
    # NULL with `is_locked`: a lock without an end.
    locked_until = Column(DateTime(timezone=True), nullable=True)

    # Audit fields
    last_login = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = Column(String(50), nullable=True, comment="Admin who created this account")

    # Profile Photo
    avatar_url = Column(String(500), nullable=True, comment="External URL or internal /v1/assets/... path")
    photo_data = Column(LargeBinary, nullable=True, comment="Uploaded photo binary")
    photo_mime_type = Column(String(100), nullable=True, comment="Photo MIME type")
    photo_size = Column(Integer, nullable=True, comment="Photo file size in bytes")
    photo_hash = Column(String(64), nullable=True, comment="SHA256 hash for ETag/dedup")

    # Two-Factor Authentication (TOTP)
    totp_secret = Column(Text, nullable=True, comment="Encrypted TOTP secret key")
    totp_enabled = Column(Boolean, default=False, nullable=False, comment="Whether 2FA is active")
    totp_verified_at = Column(DateTime(timezone=True), nullable=True, comment="When 2FA was first verified")
    backup_codes = Column(JSON, nullable=True, comment="Hashed one-time backup codes")

    # Set when the account's password was generated for it (the first
    # administrator of a tenant created without credentials): until the
    # account has changed it, every admin route but signing out, `GET /me`
    # and `POST /me/change-password` refuses it.
    password_change_required = Column(
        Boolean, default=False, nullable=False, server_default=text("false"),
        comment="The account must change its password before anything else (a generated password).",
    )

    # External identity of an administrator. Nothing writes these columns
    # in this release (SAML single sign-on is not in it); every
    # administrator signs in with a password. They stay in the schema
    # until the migrations are squashed.
    idp_server_id = Column(
        String(255),
        ForeignKey("idp_servers.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK to the SAML IdP that JIT-provisioned this admin. "
            "Lookup key is (tenant_id, idp_server_id, idp_subject)."
        ),
    )
    idp_subject = Column(
        String(512),
        nullable=True,
        comment="SAML Subject/NameID — stable identifier from the IdP.",
    )
    idp_last_login = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this admin last completed SAML SSO.",
    )

    # Relationships
    role = relationship("TenantRole", back_populates="tenant_admins")
    sessions = relationship("TenantAdminSession", back_populates="admin", cascade="all, delete-orphan")
    audit_logs = relationship("TenantAdminAuditLog", back_populates="admin")

    __table_args__ = (
        UniqueConstraint('tenant_id', 'username', name='uix_tenant_admins_tenant_username'),
        UniqueConstraint('tenant_id', 'email', name='uix_tenant_admins_tenant_email'),
        Index('ix_tenant_admins_tenant_active', 'tenant_id', 'is_active'),
        Index('ix_tenant_admins_tenant_username', 'tenant_id', 'username'),
        Index('ix_tenant_admins_tenant_email', 'tenant_id', 'email'),
        Index('ix_tenant_admins_tenant_role', 'tenant_id', 'role_id'),
        Index('ix_tenant_admins_tenant_locked', 'tenant_id', 'is_locked'),
        Index('ix_tenant_admins_tenant_failed_logins', 'tenant_id', 'failed_login_attempts'),
        Index('ix_tenant_admins_tenant_created', 'tenant_id', 'created_at'),
        Index('ix_tenant_admins_idp_lookup', 'tenant_id', 'idp_server_id', 'idp_subject'),
        {'comment': 'Tenant-scoped administrators with RBAC'}
    )

    def __repr__(self) -> str:
        return f"<TenantAdmin(id='{self.id}', tenant_id='{self.tenant_id}', username='{self.username}', role_id={self.role_id})>"

    def has_permission(self, permission_value: str) -> bool:
        """
        Check if tenant admin has specific permission via their assigned role
        """
        # No role assigned = no permissions
        if not self.role:
            return False

        # Check for SYSTEM_ADMIN permission (special case - bypasses all checks)
        role_permissions = self.role.get_permissions()

        if "system:admin" in role_permissions:
            return True  # System admin has all permissions

        # Check if permission is in role
        return permission_value in role_permissions

    def has_role(self, role_name: str) -> bool:
        """Check if tenant admin has specific role by name"""
        return self.role and self.role.name == role_name

    def get_effective_permissions(self) -> List[str]:
        """Get all effective permissions for this admin"""
        if not self.role:
            return []

        return self.role.get_permissions()

    def set_password(self, password: str) -> None:
        """Hash and set password for tenant admin"""
        from app.core.password_rule import check_password

        check_password(password)
        salt = bcrypt.gensalt()
        password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
        self.password_hash = password_hash
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "admin_type": "tenant",
            "tenant_id": self.tenant_id,
            "username": self.username,
            "email": self.email,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "is_locked": lockout.currently_locked(self),
            "role_id": self.role_id,
            "role_name": self.role.name if self.role else None,
            "roles": [self.role.name] if self.role else [],
            "permissions": self.get_effective_permissions(),
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "avatar_url": self.avatar_url,
            "totp_enabled": self.totp_enabled or False,
            "password_change_required": bool(self.password_change_required),
        }

        if include_sensitive:
            data["failed_login_attempts"] = self.failed_login_attempts

        return data


class SystemAdminSession(Base):
    """Session tracking for system administrators"""
    __tablename__ = "system_admin_sessions"

    id = Column(String(50), primary_key=True)
    admin_id = Column(String(50), ForeignKey("system_admins.id", ondelete="CASCADE"), nullable=False)

    # Session security
    token_hash = Column(String(255), nullable=False, comment="Hashed JWT token for validation")
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    risk_score = Column(Float, default=0.0, nullable=False, comment="Session risk score (0-1)")

    # Session lifecycle
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_activity = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Session state
    is_active = Column(Boolean, default=True, nullable=False)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    deactivation_reason = Column(String(100), nullable=True)

    # Relationships
    admin = relationship("SystemAdmin", back_populates="sessions")

    __table_args__ = (
        Index('ix_system_admin_sessions_token_hash', 'token_hash'),
        Index('ix_system_admin_sessions_admin_active', 'admin_id', 'is_active'),
        Index('ix_system_admin_sessions_expires', 'expires_at'),
        Index('ix_system_admin_sessions_risk', 'risk_score'),
        Index('ix_system_admin_sessions_ip', 'ip_address'),
        {'comment': 'Session tracking for system administrators'}
    )

    def __repr__(self) -> str:
        return f"<SystemAdminSession(id='{self.id}', admin_id='{self.admin_id}', active={self.is_active})>"

    def is_expired(self) -> bool:
        """Check if session is expired"""
        now = datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now > expires

    def deactivate(self, reason: str = "manual") -> None:
        """Deactivate this session"""
        self.is_active = False
        self.deactivated_at = datetime.now(timezone.utc)
        self.deactivation_reason = reason
        # The per-request read model must refuse this session immediately.
        from app.services import auth_read_model
        auth_read_model.forget_session(self.id)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "session_type": "system",
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "risk_score": self.risk_score,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "is_active": self.is_active,
            "deactivation_reason": self.deactivation_reason
        }


class TenantAdminSession(Base):
    """Session tracking for tenant administrators"""
    __tablename__ = "tenant_admin_sessions"

    id = Column(String(50), primary_key=True)
    tenant_id = Column(String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    admin_id = Column(String(50), ForeignKey("tenant_admins.id", ondelete="CASCADE"), nullable=False)

    # Session security
    token_hash = Column(String(255), nullable=False, comment="Hashed JWT token for validation")
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    risk_score = Column(Float, default=0.0, nullable=False, comment="Session risk score (0-1)")

    # Session lifecycle
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_activity = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Session state
    is_active = Column(Boolean, default=True, nullable=False)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    deactivation_reason = Column(String(100), nullable=True)

    # Relationships
    admin = relationship("TenantAdmin", back_populates="sessions")

    __table_args__ = (
        Index('ix_tenant_admin_sessions_token_hash', 'token_hash'),
        Index('ix_tenant_admin_sessions_tenant_active', 'tenant_id', 'is_active'),
        Index('ix_tenant_admin_sessions_tenant_admin_active', 'tenant_id', 'admin_id', 'is_active'),
        Index('ix_tenant_admin_sessions_tenant_expires', 'tenant_id', 'expires_at'),
        Index('ix_tenant_admin_sessions_tenant_risk', 'tenant_id', 'risk_score'),
        Index('ix_tenant_admin_sessions_tenant_ip', 'tenant_id', 'ip_address'),
        {'comment': 'Session tracking for tenant administrators'}
    )

    def __repr__(self) -> str:
        return f"<TenantAdminSession(id='{self.id}', tenant_id='{self.tenant_id}', admin_id='{self.admin_id}', active={self.is_active})>"

    def is_expired(self) -> bool:
        """Check if session is expired"""
        now = datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now > expires

    def deactivate(self, reason: str = "manual") -> None:
        """Deactivate this session"""
        self.is_active = False
        self.deactivated_at = datetime.now(timezone.utc)
        self.deactivation_reason = reason
        # The per-request read model must refuse this session immediately.
        from app.services import auth_read_model
        auth_read_model.forget_session(self.id)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "admin_id": self.admin_id,
            "session_type": "tenant",
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "risk_score": self.risk_score,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "is_active": self.is_active,
            "deactivation_reason": self.deactivation_reason
        }


class SystemAdminAuditLog(Base):
    """Audit log for system admin actions"""
    __tablename__ = "system_admin_audit_logs"

    id = Column(String(50), primary_key=True)
    admin_id = Column(String(50), ForeignKey("system_admins.id", ondelete="SET NULL"), nullable=True)

    # Action details
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(String(100), nullable=True)
    affected_tenant_id = Column(String(255), nullable=True, comment="Tenant affected by this action (if applicable)")

    # Request context
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    request_method = Column(String(10), nullable=True)
    request_path = Column(String(500), nullable=True)

    # Result
    success = Column(Boolean, nullable=False)
    details = Column(JSON, nullable=True)

    # Timestamp
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    admin = relationship("SystemAdmin", back_populates="audit_logs")

    __table_args__ = (
        Index('ix_system_admin_audit_logs_timestamp', 'timestamp'),
        Index('ix_system_admin_audit_logs_admin_timestamp', 'admin_id', 'timestamp'),
        Index('ix_system_admin_audit_logs_action_timestamp', 'action', 'timestamp'),
        Index('ix_system_admin_audit_logs_affected_tenant', 'affected_tenant_id', 'timestamp'),
        Index('ix_system_admin_audit_logs_success_timestamp', 'success', 'timestamp'),
        Index('ix_system_admin_audit_logs_ip', 'ip_address', 'timestamp'),
        {'comment': 'Audit trail for system admin actions'}
    )

    def __repr__(self) -> str:
        return f"<SystemAdminAuditLog(id='{self.id}', action='{self.action}', success={self.success})>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "admin_type": "system",
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "affected_tenant_id": self.affected_tenant_id,
            "success": self.success,
            "details": self.details or {},
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "request_method": self.request_method,
            "request_path": self.request_path,
            "timestamp": self.timestamp.isoformat()
        }


class TenantAdminAuditLog(Base):
    """Audit log for tenant admin actions"""
    __tablename__ = "tenant_admin_audit_logs"

    id = Column(String(50), primary_key=True)
    tenant_id = Column(String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    admin_id = Column(String(50), ForeignKey("tenant_admins.id", ondelete="SET NULL"), nullable=True)

    # Action details
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(String(100), nullable=True)

    # Request context
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    request_method = Column(String(10), nullable=True)
    request_path = Column(String(500), nullable=True)

    # Result
    success = Column(Boolean, nullable=False)
    details = Column(JSON, nullable=True)

    # Timestamp
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    admin = relationship("TenantAdmin", back_populates="audit_logs")

    __table_args__ = (
        Index('ix_tenant_admin_audit_logs_tenant_timestamp', 'tenant_id', 'timestamp'),
        Index('ix_tenant_admin_audit_logs_tenant_admin_timestamp', 'tenant_id', 'admin_id', 'timestamp'),
        Index('ix_tenant_admin_audit_logs_tenant_action_timestamp', 'tenant_id', 'action', 'timestamp'),
        Index('ix_tenant_admin_audit_logs_tenant_resource', 'tenant_id', 'resource_type', 'resource_id', 'timestamp'),
        Index('ix_tenant_admin_audit_logs_tenant_success_timestamp', 'tenant_id', 'success', 'timestamp'),
        Index('ix_tenant_admin_audit_logs_tenant_ip', 'tenant_id', 'ip_address', 'timestamp'),
        {'comment': 'Audit trail for tenant admin actions'}
    )

    def __repr__(self) -> str:
        return f"<TenantAdminAuditLog(id='{self.id}', tenant_id='{self.tenant_id}', action='{self.action}', success={self.success})>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "admin_id": self.admin_id,
            "admin_type": "tenant",
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "success": self.success,
            "details": self.details or {},
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "request_method": self.request_method,
            "request_path": self.request_path,
            "timestamp": self.timestamp.isoformat()
        }