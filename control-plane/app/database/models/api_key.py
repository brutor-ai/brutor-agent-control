# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
API Key Database Model
Manages API keys for accessing LLM and MCP proxy endpoints 
"""

from sqlalchemy import Column, String, Boolean, Integer, DateTime, Text, ForeignKey, Index, JSON, text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from app.database import Base


class APIKey(Base):
    """
    API keys for accessing LLM and MCP proxy endpoints

    Features:
    - Global keys (all users) or user-specific or group-specific keys
    - Expiration dates
    - Usage tracking
    - Revocation support

    **Scoping Options:**
    1. **Global** (scope='global'): Accessible by all users in the group
    2. **User** (scope='user'): Accessible only by specific user in the group
    """

    __tablename__ = "api_keys"

    # Primary identification
    id = Column(String(255), primary_key=True)

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this record belongs to"
    )

    # Key information
    key_hash = Column(
        String(255),
        nullable=False,
        comment="SHA-256 hash of the API key"
    )

    key_prefix = Column(
        String(50),
        nullable=False,
        comment="First 8 characters for display (e.g., 'brutor_sk_abcd1234...')"
    )

    name = Column(
        String(200),
        nullable=False,
        comment="Human-readable name for the key"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Optional description of the key's purpose"
    )

    scope = Column(
        String(20),
        nullable=False,
        default='global',
        comment="'global' (all users), or 'user' (specific user)"
    )

    # Agent Skills Scoping

    allowed_skill_ids = Column(
        JSON,
        nullable=True,
        comment="""Whitelist of Agent Skill IDs this API key can execute.
        NULL = all skills allowed (subject to group permissions).
        Example: ["skill-01JABCD", "skill-02KEFGH"]"""
    )

    skill_rate_limit_daily = Column(
        Integer,
        nullable=True,
        comment="Maximum skill executions per day via this API key (NULL = no key-level limit)"
    )

    # User-specific keys
    user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="User ID if scope='user'"
    )

    user_email = Column(
        String(255),
        nullable=True,
        comment="User email for display purposes"
    )

    # Agent principal this key authenticates as (Agent Identity). When set, a
    # request bearing this key is the *actor* = this agent; the runtime authz
    # engine evaluates the agent's grants action-by-action. The key goes
    # with the agent (ON DELETE CASCADE): with SET NULL the key of a
    # deleted agent would be a working plain key of its resource group.
    agent_id = Column(
        String(255),
        ForeignKey("agent_identities.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Agent identity this key acts as, if any"
    )

    # Resource Group key is valid for
    group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="ID for Resource Group"
    )

    expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this key expires. Null = never expires"
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether the key is currently enabled"
    )

    # The platform's own service key (`system-health-check`). Set only
    # by the control plane's seed; no admin API model carries it. The core
    # bills its calls to the tenant's `Unbound` system group and refuses any
    # other key that has no resource group (`group_id IS NULL`).
    is_platform_service = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Platform service key (control plane / KB uploader); only the seed sets it"
    )

    revoked = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether the key has been revoked"
    )

    revoked_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the key was revoked"
    )

    revoked_by = Column(
        String(255),
        nullable=True,
        comment="Admin ID who revoked the key"
    )

    revoked_reason = Column(
        Text,
        nullable=True,
        comment="Reason for revocation"
    )

    last_used_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time this key was used"
    )

    last_used_by_user_id = Column(
        Integer,
        nullable=True,
        comment="Last user who used this key"
    )

    usage_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total number of requests made with this key"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    created_by = Column(
        String(255),
        nullable=True,
        comment="Admin ID who created the key"
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    updated_by = Column(
        String(255),
        nullable=True,
        comment="Admin ID who last updated the key"
    )

    __table_args__ = (
        # Tenant queries
        Index('ix_api_keys_tenant_enabled', 'tenant_id', 'enabled'),
        Index('ix_api_keys_tenant_scope', 'tenant_id', 'scope'),

        # Key lookup (for authentication)
        Index('ix_api_keys_key_hash', 'key_hash'),
        Index('ix_api_keys_key_prefix', 'key_prefix'),

        # User-specific keys
        Index('ix_api_keys_tenant_user', 'tenant_id', 'user_id'),

        # Group-specific keys
        Index('ix_api_keys_tenant_group', 'tenant_id', 'group_id'),
        Index('ix_api_keys_tenant_group_enabled', 'tenant_id', 'group_id', 'enabled'),

        # Status queries
        Index('ix_api_keys_tenant_revoked', 'tenant_id', 'revoked'),
        Index('ix_api_keys_tenant_expires', 'tenant_id', 'expires_at'),

        # Usage tracking
        Index('ix_api_keys_last_used', 'last_used_at'),
        {
            'comment': 'API keys for LLM and MCP proxy access'
        }
    )

    # End User relationship (for user scoped keys)
    user = relationship("EndUser", back_populates="api_keys")

    # Resource Group relationship
    group = relationship("ResourceGroup", back_populates="api_keys")

    @property
    def is_valid(self) -> bool:
        """Check if key is currently valid"""
        if not self.enabled or self.revoked:
            return False

        if self.expires_at and self.expires_at < datetime.now(timezone.utc):
            return False

        return True

    @property
    def is_expired(self) -> bool:
        """Check if key has expired"""
        if not self.expires_at:
            return False
        return self.expires_at < datetime.now(timezone.utc)

    @property
    def is_global(self) -> bool:
        """Check if key is global (accessible by all users)"""
        return self.scope == 'global'

    @property
    def is_user_specific(self) -> bool:
        """Check if key is tied to a specific user"""
        return self.scope == 'user' and self.user_id is not None

    def can_be_used_by_user(self, user_id: int, user_groups: List[str]) -> bool:
        """
        Check if key can be used by a specific user

        Args:
            user_id: User ID attempting to use the key
            user_groups: List of group IDs the user belongs to

        Returns:
            True if user can use this key
        """
        # Global keys - anyone can use
        if self.is_global:
            return True

        # User-specific keys - only that user
        if self.is_user_specific:
            return self.user_id == user_id

        # Group-specific keys - check if user is in group or descendant
        if self.is_group_specific:
            # Direct membership
            if self.group_id in user_groups:
                return True

            # Check if user is in a descendant group
            if self.group:
                descendant_ids = [g.id for g in self.group.get_descendants()]
                return any(gid in user_groups for gid in descendant_ids)

        return False

    def can_execute_skill(self, skill_id: str) -> bool:
        """
        Check if this API key is allowed to execute a specific skill

        Args:
            skill_id: The Agent Skill ID to check

        Returns:
            True if execution is allowed
        """
        if self.allowed_skill_ids is None:
            return True  # NULL = all skills allowed
        return skill_id in self.allowed_skill_ids

    def get_allowed_skill_ids(self) -> Optional[list]:
        """Get list of allowed skill IDs (None means all allowed)"""
        return self.allowed_skill_ids

    def record_usage(self, tokens_consumed: int = 0, user_id: Optional[int] = None) -> None:
        """
        Record API key usage

        Args:
            tokens_consumed: Number of tokens consumed
            user_id: ID of user who used the key
        """
        self.usage_count += 1
        self.last_used_at = datetime.now(timezone.utc)
        if user_id:
            self.last_used_by_user_id = user_id
        self.updated_at = datetime.now(timezone.utc)

    def revoke(self, admin_id: str, reason: Optional[str] = None) -> None:
        """Revoke the API key"""
        self.revoked = True
        self.enabled = False
        self.revoked_at = datetime.now(timezone.utc)
        self.revoked_by = admin_id
        self.revoked_reason = reason
        self.updated_at = datetime.now(timezone.utc)

    def enable_key(self) -> None:
        """Enable the key (only if not revoked)"""
        if not self.revoked:
            self.enabled = True
            self.updated_at = datetime.now(timezone.utc)

    def disable_key(self) -> None:
        """Disable the key temporarily"""
        self.enabled = False
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "key_prefix": self.key_prefix,
            "name": self.name,
            "description": self.description,

            # Scope
            "scope": self.scope,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "agent_id": self.agent_id,
            "group_id": self.group_id,
            "group_name": self.group.name if self.group else None,
            "group_type": self.group.group_type.value if self.group and self.group.group_type else None,

            # Expiration
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_expired": self.is_expired,

            # Status
            "enabled": self.enabled,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_by": self.revoked_by,
            "revoked_reason": self.revoked_reason,
            "is_valid": self.is_valid,

            # Usage
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_used_by_user_id": self.last_used_by_user_id,
            "usage_count": self.usage_count,

            # Metadata
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "updated_at": self.updated_at.isoformat(),
            "updated_by": self.updated_by,

            # Agent skills
            "allowed_skill_ids": self.allowed_skill_ids,
            "skill_rate_limit_daily": self.skill_rate_limit_daily,
        }

        # Only include sensitive data if explicitly requested (e.g., for auditing)
        if include_sensitive:
            data["key_hash"] = self.key_hash

        return data

    def to_summary_dict(self) -> Dict[str, Any]:
        """Convert to summary dictionary for list views"""
        return {
            "id": self.id,
            "key_prefix": self.key_prefix,
            "name": self.name,
            "scope": self.scope,
            "user_email": self.user_email if self.is_user_specific else None,
            "group_id": self.group_id,
            "group_name": self.group.name if self.group else None,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "revoked": self.revoked,
            "is_valid": self.is_valid,
            "is_expired": self.is_expired,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "usage_count": self.usage_count,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat()
        }