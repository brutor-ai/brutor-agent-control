# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End User Model
Represents end users who access MCP servers through the gateway
"""

import json
from datetime import datetime, timezone
from typing import Dict, Any, List

from sqlalchemy import Column, Text, Integer, String, Boolean, DateTime, Index, UniqueConstraint, ForeignKey, JSON, LargeBinary
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# `end_users.source` of a user a traffic import created: somebody found in a
# vendor's usage report, who holds no password and cannot sign in.
SOURCE_IMPORT = "import"


class EndUser(Base):
    """
    End user accounts for access to AI Models and MCP servers

    End users are distinct from admin users - they:
    - Access AI Models and MCP servers through the gateway proxy
    - Authenticate via JWT tokens
    - Have usage tracked in proxy logs
    - Are assigned a TenantRole (typically "Tenant User")
    - Get permissions from their assigned role
    """
    __tablename__ = "end_users"

    # Primary Key
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # Role assignment - links to TenantRole
    role_id = Column(
        String(50),
        ForeignKey("tenant_roles.id", ondelete="SET NULL"),
        nullable=True,  # Can be NULL initially, but should be assigned during creation
        comment="Role assigned to this end user (determines permissions)"
    )

    # Primary resource group — single active scope at runtime.
    # Stamped onto every proxy/portal request via X-Resource-Group header.
    # Set automatically when the first group membership is created; can be
    # changed explicitly by an admin or by the user via the portal group
    # switcher. NULL only for users with no group memberships yet.
    primary_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment="Primary resource group used as default runtime scope"
    )

    # Identity
    username = Column(
        String(100),
        nullable=False,
        comment="Unique username within tenant for login"
    )

    email = Column(
        String(255),
        nullable=True,
        comment="User email address"
    )

    display_name = Column(
        String(255),
        nullable=True,
        comment="Display name for UI"
    )

    # Authentication
    password_hash = Column(
        String(255),
        nullable=False,
        comment="Bcrypt hashed password"
    )

    # Status
    is_verified = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether email is verified"
    )

    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether account is active"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Account creation timestamp"
    )

    last_login = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last successful login timestamp"
    )

    # Metadata
    user_metadata = Column(
        JSON,
        nullable=True,
        comment="JSON string for additional user data"
    )

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
    sessions_valid_after = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Portal tokens issued before this are refused (set on password change)",
    )

    # ==========================================================================
    # External identity. Nothing writes these columns in this release
    # (SAML single sign-on and SCIM provisioning are not in it): every end
    # user is created by an administrator or by a traffic import. They
    # stay in the schema until the migrations are squashed.
    # ==========================================================================
    idp_server_id = Column(
        String(255),
        ForeignKey("idp_servers.id", ondelete="SET NULL"),
        nullable=True,
        comment="IdP that provisioned this user (null = password auth)",
    )
    idp_subject = Column(
        String(500),
        nullable=True,
        comment="External NameID (SAML) or sub claim (OIDC)",
    )
    idp_last_login = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Most recent SSO completion (distinct from last_login)",
    )

    # Provenance (Traffic Data Import). 'manual' (default) |
    # 'import' (discovered from a vendor usage-import, not a login).
    source = Column(
        String(16),
        server_default="manual",
        default="manual",
        nullable=False,
        comment="manual | saml | scim | import",
    )
    source_external_id = Column(
        String(255),
        nullable=True,
        comment="External id/email this user was discovered under (import)",
    )

    # Relationships
    role = relationship("TenantRole", foreign_keys=[role_id])

    # Many-to-many with ResourceGroup through user_group_memberships
    group_memberships = relationship(
        "ResourceGroupMembership",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # Many-to-many with EndUserGroup: named groups that in
    # turn bind to Resource Groups. See
    # `app/database/models/end_user_group.py` for the design rationale.
    user_groups = relationship(
        "EndUserGroup",
        secondary="end_user_group_members",
        back_populates="members",
        lazy="selectin",
    )

    # One-to-many with APIKey
    api_keys = relationship(
        "APIKey",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # Indexes for common queries
    __table_args__ = (
        # Usernames and emails must be unique within tenant, not globally
        UniqueConstraint('tenant_id', 'username', name='uix_end_users_tenant_username'),
        UniqueConstraint('tenant_id', 'email', name='uix_end_users_tenant_email'),

        # Primary queries - "Get all active users for tenant"
        Index('ix_end_users_tenant_active', 'tenant_id', 'is_active'),

        # Primary-group lookup, the same index the migration creates.
        Index('ix_end_users_tenant_primary_group', 'tenant_id', 'primary_group_id'),

        # User lookup by username within tenant
        Index('ix_end_users_tenant_username', 'tenant_id', 'username'),

        # User lookup by email within tenant
        Index('ix_end_users_tenant_email', 'tenant_id', 'email'),

        # Verified users - "Get verified users"
        Index('ix_end_users_tenant_verified', 'tenant_id', 'is_verified'),

        # Combined status - "Get active and verified users"
        Index('ix_end_users_tenant_active_verified', 'tenant_id', 'is_active', 'is_verified'),

        # Recent logins - "Show recently active users"
        Index('ix_end_users_tenant_last_login', 'tenant_id', 'last_login'),

        # Audit queries - "Show recent user registrations"
        Index('ix_end_users_tenant_created', 'tenant_id', 'created_at'),

        # Role-based queries - "Get all users with specific role"
        Index('ix_end_users_tenant_role', 'tenant_id', 'role_id'),

        {
            'comment': 'End users who access MCP servers through the gateway'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<EndUser(id={self.id}, username='{self.username}', "
            f"role_id='{self.role_id}', is_active={self.is_active})>"
        )

    def has_permission(self, permission_value: str) -> bool:
        """
        Check if end user has specific permission via their assigned role

        Args:
            permission_value: Permission string (e.g., 'ai:model:read')

        Returns:
            True if user has permission, False otherwise
        """
        # No role assigned = no permissions
        if not self.role:
            return False

        # Check if permission is in role
        role_permissions = self.role.get_permissions()
        return permission_value in role_permissions

    def has_role(self, role_name: str) -> bool:
        """
        Check if end user has specific role by name

        Args:
            role_name: Role name to check

        Returns:
            True if user has this role, False otherwise
        """
        return self.role and self.role.name == role_name

    def get_effective_permissions(self) -> List[str]:
        """
        Get all effective permissions for this end user

        Returns:
            List of permission strings
        """
        if not self.role:
            return []

        return self.role.get_permissions()

    def get_groups(self) -> List[str]:
        """
        Get list of group IDs this user belongs to

        Returns:
            List of group IDs
        """
        return [membership.group_id for membership in self.group_memberships.all()]

    def is_member_of(self, group_id: str) -> bool:
        """
        Check if user is a member of a specific group

        Args:
            group_id: Group ID to check

        Returns:
            True if user is a member, False otherwise
        """
        return self.group_memberships.filter_by(group_id=group_id).first() is not None

    @classmethod
    def count_for_quota(cls, db, tenant_id: str) -> int:
        """The tenant's end users as the end-user quota and the licence's
        end-user limit count them: everyone but the users a traffic import
        created, who cannot sign in and exist so that imported usage has an
        owner."""
        return db.query(cls).filter(cls.tenant_id == tenant_id, cls.source != SOURCE_IMPORT).count()

    @classmethod
    def count_imported(cls, db, tenant_id: str) -> int:
        """The tenant's end users a traffic import created."""
        return db.query(cls).filter(cls.tenant_id == tenant_id, cls.source == SOURCE_IMPORT).count()

    def to_dict(self, include_metadata: bool = True, include_groups: bool = False) -> Dict[str, Any]:
        """
        Convert end user to dictionary for API responses

        Args:
            include_metadata: Whether to include user_metadata field
            include_groups: Whether to include group membership information

        Returns:
            Dictionary representation of the user
        """
        data = {
            "id": str(self.id),
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "is_verified": self.is_verified,
            "is_active": self.is_active,
            "role_id": self.role_id,
            "role_name": self.role.name if self.role else None,
            "permissions": self.get_effective_permissions(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "avatar_url": self.avatar_url,
            "totp_enabled": self.totp_enabled or False,
            "primary_group_id": self.primary_group_id,
            "source": self.source,
            "source_external_id": self.source_external_id,
        }

        if include_metadata and self.user_metadata:
            if isinstance(self.user_metadata, dict):
                data["metadata"] = self.user_metadata
            else:
                try:
                    data["metadata"] = json.loads(self.user_metadata)
                except (json.JSONDecodeError, TypeError):
                    data["metadata"] = {}
        elif include_metadata:
            data["metadata"] = {}

        if include_groups:
            data["groups"] = self.get_groups()
            data["group_count"] = len(data["groups"])

        return data

    def get_metadata(self) -> Dict[str, Any]:
        """
        Get parsed user metadata

        Returns:
            Parsed metadata dictionary or empty dict if none
        """
        if not self.user_metadata:
            return {}

        try:
            return json.loads(self.user_metadata)
        except json.JSONDecodeError:
            return {}

    def set_metadata(self, metadata: Dict[str, Any]) -> None:
        """
        Set user metadata from dictionary

        Args:
            metadata: Dictionary to store as JSON
        """
        self.user_metadata = json.dumps(metadata)

    def update_metadata(self, updates: Dict[str, Any]) -> None:
        """
        Update specific keys in user metadata

        Args:
            updates: Dictionary of keys to update
        """
        current = self.get_metadata()
        current.update(updates)
        self.set_metadata(current)

    def mark_login(self) -> None:
        """Update last_login timestamp to now"""
        self.last_login = datetime.now(timezone.utc)

    def activate(self) -> None:
        """Activate user account"""
        self.is_active = True

    def deactivate(self) -> None:
        """Deactivate user account"""
        self.is_active = False

    def verify_email(self) -> None:
        """Mark email as verified"""
        self.is_verified = True

    @classmethod
    def create_from_dict(cls, data: Dict[str, Any]) -> 'EndUser':
        """
        Create a new EndUser instance from a dictionary

        Args:
            data: Dictionary containing user data

        Returns:
            New EndUser instance (not yet added to session)
        """
        return cls(
            username=data['username'],
            email=data.get('email'),
            display_name=data.get('display_name'),
            password_hash=data['password_hash'],
            role_id=data.get('role_id'),  # Should be set to "Tenant User" role by default
            is_verified=data.get('is_verified', False),
            is_active=data.get('is_active', True),
            avatar_url=data.get('avatar_url'),
            user_metadata=json.dumps(data['metadata']) if 'metadata' in data else None
        )