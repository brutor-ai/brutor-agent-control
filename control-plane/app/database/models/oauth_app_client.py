# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth App Client Model
Represents OAuth client credentials for applications (webapp, mobile, desktop, etc.)
that connect to MCP servers through the gateway
"""

from sqlalchemy import Column, String, Boolean, DateTime, Text, JSON, Index, UniqueConstraint, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, validates
from datetime import datetime, timezone
from typing import List, Dict, Any

from app.constants.oauth import ClientType
from app.database import Base


class OAuthAppClient(Base):
    """
    OAuth App Client model.

    Represents OAuth client credentials for an application (not per-user).
    These clients are registered with Identity Providers and can be assigned
    to one or more MCP servers through ServerConfig.

    Examples: webapp client, mobile app client, desktop app client
    """
    __tablename__ = "oauth_app_clients"

    # Primary Key
    id = Column(String(255), primary_key=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # Client Identification
    name = Column(String(255), nullable=False, comment="Friendly name (e.g., 'Web App Client', 'iOS App')")
    app_type = Column(String(50), nullable=False, comment="Application type: webapp, mobile, desktop, cli")

    # Identity Provider Association
    idp_id = Column(
        String(255),
        ForeignKey("idp_servers.id", ondelete="CASCADE"),
        nullable=False,
        comment="Identity Provider ID where this client is registered"
    )

    # OAuth Client Credentials (from IdP)
    client_id = Column(String(500), nullable=False, comment="OAuth client ID from IdP")
    encrypted_client_secret = Column(Text, nullable=True,
                                     comment="Encrypted OAuth client secret (null for public clients)")

    # OAuth Configuration
    redirect_uri = Column(Text, nullable=False, comment="OAuth redirect URI registered with IdP")
    scopes = Column(JSON, nullable=False, default=list, comment="Default OAuth scopes for this client")
    client_type = Column(String(50), nullable=False, comment="confidential or public")

    # Client Capabilities
    supports_pkce = Column(Boolean, nullable=False, default=True, comment="Whether this client uses PKCE")
    allowed_grant_types = Column(JSON, nullable=False, default=list, comment="Allowed OAuth grant types")

    # Metadata
    description = Column(Text, nullable=True, comment="Optional description of this OAuth client")

    # Audit Fields
    registered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    registered_by = Column(String(50), nullable=True, comment="AdminUser.id who registered this client")
    last_used = Column(DateTime(timezone=True), nullable=True, comment="Last time this client was used in OAuth flow")

    # Status
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)

    # Dynamic Client Registration (RFC 7591)
    auto_registered = Column(Boolean, default=False, server_default="false", nullable=False, comment="Created via RFC 7591 Dynamic Client Registration")
    registration_client_uri = Column(String(500), nullable=True, comment="URI for managing the dynamically registered client")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    # Relationship to IdPServer (many-to-one)
    idp = relationship(
        "IdPServer",
        foreign_keys=[idp_id],
        lazy="joined"  # Load IdP data automatically with the client
    )

    # Reverse relationship to ServerConfig (one client can be used by many server configs)
    server_configs = relationship(
        "ServerConfig",
        back_populates="oauth_client",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # Table Constraints and Indexes
    __table_args__ = (
        # Unique constraint must include tenant_id
        # One IdP can have the same client_id across different tenants
        UniqueConstraint('tenant_id', 'idp_id', 'client_id',
                        name='uix_oauth_app_clients_tenant_idp_client'),

        # Primary queries - "Get all OAuth clients for a tenant"
        Index('ix_oauth_app_clients_tenant_active', 'tenant_id', 'is_active'),

        # Client lookup by name within tenant
        Index('ix_oauth_app_clients_tenant_name', 'tenant_id', 'name'),

        # App type filtering - "Get all mobile clients"
        Index('ix_oauth_app_clients_tenant_app_type', 'tenant_id', 'app_type'),
        Index('ix_oauth_app_clients_tenant_app_type_active', 'tenant_id', 'app_type', 'is_active'),

        # IdP-based queries - "Get all clients for an IdP"
        Index('ix_oauth_app_clients_tenant_idp', 'tenant_id', 'idp_id'),
        Index('ix_oauth_app_clients_tenant_idp_active', 'tenant_id', 'idp_id', 'is_active'),

        # Usage tracking - "Find recently used or unused clients"
        Index('ix_oauth_app_clients_tenant_last_used', 'tenant_id', 'last_used'),

        # Audit queries
        Index('ix_oauth_app_clients_created_at', 'tenant_id', 'created_at'),

        {
            'comment': 'OAuth application clients registered with Identity Providers'
        }
    )

    @validates('app_type')
    def validate_app_type(self, key, value):
        """Validate app_type"""
        valid_types = ['webapp', 'mobile', 'desktop', 'cli', 'service']
        if value not in valid_types:
            raise ValueError(f"app_type must be one of: {', '.join(valid_types)}")
        return value

    @validates('client_type')
    def validate_client_type(self, key, value):
        """Validate client_type"""
        if value not in ClientType:
            valid_types = ", ".join([ct.value for ct in ClientType])
            raise ValueError(f"client_type must be one of: {valid_types}")
        return value

    @validates('redirect_uri')
    def validate_redirect_uri(self, key, value):
        """Validate redirect URI format"""
        if not value.startswith(('http://', 'https://')):
            raise ValueError("Redirect URI must start with http:// or https://")
        return value

    def __repr__(self) -> str:
        return f"<OAuthAppClient(id='{self.id}', name='{self.name}', app_type='{self.app_type}', idp_id='{self.idp_id}')>"

    def mark_as_used(self) -> None:
        """Update the last_used timestamp to current time"""
        self.last_used = datetime.now(timezone.utc)

    def is_confidential(self) -> bool:
        """Check if this is a confidential client"""
        return self.client_type == ClientType.CONFIDENTIAL.value

    def is_public(self) -> bool:
        """Check if this is a public client"""
        return self.client_type == ClientType.PUBLIC.value

    def is_public_with_secret(self) -> bool:
        """Check if this is a public client (PKCE) with secret"""
        return self.client_type == ClientType.PUBLIC_WITH_SECRET.value

    def has_secret(self) -> bool:
        """Check if this client has a secret configured"""
        return self.encrypted_client_secret is not None and len(self.encrypted_client_secret) > 0

    def get_scopes_list(self) -> List[str]:
        """Get scopes as a list"""
        return self.scopes if isinstance(self.scopes, list) else []

    def set_scopes_list(self, scopes: List[str]) -> None:
        """Set scopes from a list"""
        self.scopes = scopes if scopes else []

    def validate(self) -> List[str]:
        """
        Validate the OAuth client configuration

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if not self.name or not self.name.strip():
            errors.append("Client name is required")

        if not self.idp_id or not self.idp_id.strip():
            errors.append("Identity Provider (idp_id) is required")

        if not self.client_id or not self.client_id.strip():
            errors.append("OAuth client_id is required")

        if not self.redirect_uri or not self.redirect_uri.strip():
            errors.append("Redirect URI is required")
        elif not self.redirect_uri.startswith(('http://', 'https://')):
            errors.append("Redirect URI must be a valid HTTP/HTTPS URL")

        if self.client_type not in ClientType:
            errors.append(f"Client type must be {ClientType.CONFIDENTIAL.value}, {ClientType.PUBLIC.value}, or {ClientType.PUBLIC_WITH_SECRET.value}")

        if self.client_type == ClientType.CONFIDENTIAL.value and not self.has_secret():
            errors.append("Confidential clients must have a client_secret")

        if self.client_type == ClientType.PUBLIC_WITH_SECRET.value and not self.has_secret():
            errors.append("Public PKCE clients with secret must have a client_secret")

        if self.client_type == ClientType.PUBLIC.value and self.has_secret():
            errors.append("Public PKCE clients cannot have a client_secret")

        if not isinstance(self.scopes, list):
            errors.append("Scopes must be a list")

        if self.app_type not in ['webapp', 'mobile', 'desktop', 'cli', 'service']:
            errors.append("Invalid app_type")

        return errors

    def to_dict(self, include_secret: bool = False) -> Dict[str, Any]:
        """
        Convert OAuth client to dictionary representation

        Args:
            include_secret: Whether to include encrypted_client_secret

        Returns:
            Dictionary representation
        """
        data = {
            'id': self.id,
            'name': self.name,
            'app_type': self.app_type,
            'idp_id': self.idp_id,
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'scopes': self.get_scopes_list(),
            'client_type': self.client_type,
            'supports_pkce': self.supports_pkce,
            'allowed_grant_types': self.allowed_grant_types or [],
            'description': self.description,
            'registered_at': self.registered_at.isoformat() if self.registered_at else None,
            'registered_by': self.registered_by,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'is_active': self.is_active,
            'auto_registered': self.auto_registered,
            'registration_client_uri': self.registration_client_uri,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

        if include_secret and self.encrypted_client_secret:
            data['encrypted_client_secret'] = self.encrypted_client_secret

        return data
