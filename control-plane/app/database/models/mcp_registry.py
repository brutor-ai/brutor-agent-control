# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Registry Database Model
Stores MCP server registry configurations
"""

from datetime import datetime, timezone
from typing import Dict, Any, List

from sqlalchemy import Column, String, Boolean, Integer, Text, DateTime, Index, ForeignKey
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base


class MCPRegistry(Base):
    """
    MCP Registry configuration for server discovery

    Registries allow discovery of publicly available MCP servers
    from central repositories.
    """
    __tablename__ = "mcp_registry_configs"

    # Primary identification
    id = Column(String(255), primary_key=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    name = Column(String(255), nullable=False)
    url = Column(String(500), nullable=False)

    # Authentication
    auth_type = Column(String(50), nullable=False, default="none")
    # Ciphertext of the control plane's encryption service; only the control
    # plane reads it (to call the registry) and no API answer carries it.
    auth_token = Column(Text, nullable=True)

    # Configuration
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    verified = Column(Boolean, nullable=False, default=False)
    timeout = Column(Integer, nullable=False, default=10)
    is_default = Column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_sync = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Primary queries - "Get all enabled registries for tenant"
        Index('ix_mcp_registry_tenant_enabled', 'tenant_id', 'enabled'),

        # Registry lookup by name within tenant
        Index('ix_mcp_registry_tenant_name', 'tenant_id', 'name'),

        # Verified registries - "Get verified registries"
        Index('ix_mcp_registry_tenant_verified', 'tenant_id', 'verified'),

        # Default registry lookup - "Get default registry for tenant"
        Index('ix_mcp_registry_tenant_default', 'tenant_id', 'is_default'),

        # Combined filter - "Get enabled and verified registries"
        Index('ix_mcp_registry_tenant_en_ver', 'tenant_id', 'enabled', 'verified'),

        # Sync tracking - "Find registries needing sync"
        Index('ix_mcp_registry_tenant_last_sync', 'tenant_id', 'last_sync', 'enabled'),

        # Audit queries
        Index('ix_mcp_registry_created_at', 'tenant_id', 'created_at'),

        {
            'comment': 'MCP server registry configurations for discovery'
        }
    )

    def __repr__(self) -> str:
        return f"<MCPRegistry(id='{self.id}', name='{self.name}', enabled={self.enabled})>"

    @validates('auth_type')
    def validate_auth_type(self, key: str, value: str) -> str:
        """Validate authentication type"""
        allowed = ['none', 'bearer', 'api_key']
        if value not in allowed:
            raise ValueError(f"auth_type must be one of {allowed}")
        return value

    @validates('url')
    def validate_url(self, key: str, value: str) -> str:
        """Validate URL format"""
        if not value.startswith(('http://', 'https://')):
            raise ValueError("URL must start with http:// or https://")
        return value

    @validates('timeout')
    def validate_timeout(self, key: str, value: int) -> int:
        """Validate timeout value"""
        if value <= 0 or value > 300:
            raise ValueError("timeout must be between 0 and 300 seconds")
        return value

    def mark_synced(self) -> None:
        """Update last sync timestamp"""
        self.last_sync = datetime.now(timezone.utc)

    def enable(self) -> None:
        """Enable the registry"""
        self.enabled = True
        self.updated_at = datetime.now(timezone.utc)

    def disable(self) -> None:
        """Disable the registry"""
        self.enabled = False
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Returns:
            Dictionary representation
        """
        data = {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "auth_type": self.auth_type,
            "description": self.description,
            "enabled": self.enabled,
            "verified": self.verified,
            "timeout": self.timeout,
            "default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None
        }

        # Whether a token is stored; the token itself is never part of an
        # answer, in any form.
        data["has_auth_token"] = bool(self.auth_token)

        return data

    def validate(self) -> List[str]:
        """
        Validate registry configuration

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if not self.id or not self.id.strip():
            errors.append("Registry ID is required")

        if not self.name or not self.name.strip():
            errors.append("Registry name is required")

        if not self.url or not self.url.strip():
            errors.append("Registry URL is required")
        elif not self.url.startswith(('http://', 'https://')):
            errors.append("Registry URL must start with http:// or https://")

        if self.auth_type not in ['none', 'bearer', 'api_key']:
            errors.append("Invalid auth_type")

        if self.auth_type in ['bearer', 'api_key'] and not self.auth_token:
            errors.append(f"{self.auth_type} auth requires auth_token")

        if self.timeout <= 0 or self.timeout > 300:
            errors.append("Timeout must be between 0 and 300 seconds")

        return errors