# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
OAuth Token Model
Stores encrypted OAuth access and refresh tokens
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import Column, String, Text, DateTime, Integer, Index, UniqueConstraint, Boolean, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import validates

from app.database import Base


class OAuthToken(Base):
    """
    OAuth Token storage with encryption

    Stores OAuth access and refresh tokens with encryption at rest.
    Used for automatic token injection and refresh in proxy requests.
    """
    __tablename__ = "oauth_tokens"

    # Primary Key
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # Brutor Gateway End-user ID, for tracing, a gateway end-user can have many tokens for different IdPs
    # This can be null if user is not going through gateway authentication
    gateway_user_id = Column(
        String(255),
        nullable=True,
        comment="Gateway User ID for tracing"
    )

    # OAuth Identity Provider User ID
    idp_user_id = Column(
        String(255),
        nullable=False,
        comment="IdP User ID who owns this token"
    )

    # MCP Server ID that this token is valid for
    mcp_server_id = Column(
        String(255),
        nullable=False,
        comment="MCP server this token is for"
    )

    # IdP Server ID that this token is valid for
    idp_id = Column(
        String(255),
        nullable=False,
        comment="Identity Provider ID"
    )

    # OAuth client credentials
    client_id = Column(
        String(500),
        nullable=True,
        comment="OAuth client ID"
    )

    # Store encrypted client secret for refresh token flow
    encrypted_client_secret = Column(
        Text,
        nullable=True,
        comment="Encrypted OAuth client secret (needed for refresh)"
    )

    # Encrypted access_token used to authenticate requests with the MCP Server
    encrypted_access_token = Column(
        Text,
        nullable=False,
        comment="Encrypted OAuth access token"
    )

    encrypted_refresh_token = Column(
        Text,
        nullable=True,
        comment="Encrypted OAuth refresh token"
    )

    # Token metadata
    token_type = Column(
        String(50),
        nullable=False,
        default="Bearer",
        comment="Token type (usually Bearer)"
    )

    expires_at = Column(
        DateTime,
        nullable=False,
        comment="When the access token expires"
    )

    scopes = Column(
        Text,
        nullable=True,
        comment="Comma-separated list of granted scopes"
    )

    issuer = Column(
        String(500),
        nullable=True,
        comment=(
            "MCP SEP-2352 — the IdP issuer this token was "
            "minted under. The gateway refuses to refresh a token against "
            "a different issuer (issuer binding). NULL: no issuer recorded."
        )
    )

    revoked = Column(Boolean, default=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Tracking
    created_at = Column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="When token was first stored"
    )

    last_refreshed_at = Column(
        DateTime,
        nullable=True,
        comment="Last time token was refreshed"
    )

    refresh_count = Column(
        Integer,
        default=0,
        nullable=False,
        comment="Number of times token has been refreshed"
    )

    # Agent on-behalf-of attribution: the agent principal that most recently
    # exercised this (user-held) token via the secretless broker. Complements
    # the per-request proxy_logs.actor_agent_id with a broker-side record.
    last_actor_agent_id = Column(
        String(255),
        nullable=True,
        comment="Agent principal that last used this token on behalf of the user"
    )
    last_used_by_agent_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When an agent last exercised this token"
    )

    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Last update timestamp"
    )

    # Constraints and Indexes
    __table_args__ = (
        # One credential per tenant, identity provider, that provider's
        # subject and MCP server (revision `a217_oauth_token_idp_key`). A subject is unique
        # within its provider only: without `idp_id` two providers naming
        # the same subject met on one row. The key's own index serves the
        # keyed lookup ("this provider's user's token for this server").
        UniqueConstraint(
            'tenant_id', 'idp_id', 'idp_user_id', 'mcp_server_id',
            name='uq_oauth_tokens_tenant_idp_user_server'
        ),

        # User's tokens - "Get all tokens for a Gateway user"
        Index('ix_oauth_tokens_tenant_gateway_user', 'tenant_id', 'gateway_user_id'),

        # User's tokens - "Get all tokens for an IdP user"
        Index('ix_oauth_tokens_tenant_idp_user', 'tenant_id', 'idp_user_id'),

        # Server tokens - "Get all tokens for a server"
        Index('ix_oauth_tokens_tenant_server', 'tenant_id', 'mcp_server_id'),

        # IdP tokens - "Get all tokens for an IdP"
        Index('ix_oauth_tokens_tenant_idp', 'tenant_id', 'idp_id'),

        # Expiration management - "Find expiring/expired tokens"
        Index('ix_oauth_tokens_tenant_expires', 'tenant_id', 'expires_at', 'revoked'),

        # Revoked tokens - "Find revoked tokens for cleanup"
        Index('ix_oauth_tokens_tenant_revoked', 'tenant_id', 'revoked', 'revoked_at'),

        # Token refresh queries - "Find tokens needing refresh"
        Index('ix_oauth_tokens_refresh_tracking', 'tenant_id', 'expires_at', 'revoked'),

        {
            'comment': 'OAuth tokens with encryption at rest'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<OAuthToken(id={self.id}, user_id='{self.idp_user_id}', "
            f"mcp_server_id='{self.mcp_server_id}', idp_id='{self.idp_id}')>"
        )

    @validates('token_type')
    def validate_token_type(self, key: str, value: str) -> str:
        """Validate token type"""
        if value not in ['Bearer', 'DPoP', 'MAC']:
            raise ValueError(f"Invalid token type: {value}")
        return value

    def is_expired(self) -> bool:
        """Check if token is expired"""
        return datetime.now(timezone.utc) >= self.expires_at

    def is_near_expiry(self, minutes: int = 5) -> bool:
        """Check if token will expire soon"""
        from datetime import timedelta
        return datetime.now(timezone.utc) >= (self.expires_at - timedelta(minutes=minutes))

    def get_scopes_list(self) -> List[str]:
        """
        Get scopes as a list

        Returns:
            List of scope strings
        """
        if not self.scopes:
            return []
        return [s.strip() for s in self.scopes.split(',') if s.strip()]

    def set_scopes_list(self, scopes: List[str]) -> None:
        """
        Set scopes from a list

        Args:
            scopes: List of scope strings
        """
        self.scopes = ','.join(scopes) if scopes else None

    def mark_refreshed(self) -> None:
        """Update refresh tracking"""
        self.last_refreshed_at = datetime.now(timezone.utc)
        self.refresh_count += 1

    def to_dict(self, include_tokens: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_tokens: Whether to include encrypted tokens

        Returns:
            Dictionary representation
        """
        data = {
            "id": str(self.id),
            "gateway_user_id": self.gateway_user_id,
            "idp_user_id": self.idp_user_id,
            "mcp_server_id": self.mcp_server_id,
            "idp_id": self.idp_id,
            "client_id": self.client_id,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat(),
            "is_expired": self.is_expired(),
            "scopes": self.get_scopes_list(),
            "revoked": self.revoked,
            "has_refresh_token": self.encrypted_refresh_token is not None,
            "last_refreshed_at": (
                self.last_refreshed_at.isoformat()
                if self.last_refreshed_at else None
            ),
            "refresh_count": self.refresh_count,
            "last_actor_agent_id": self.last_actor_agent_id,
            "last_used_by_agent_at": (
                self.last_used_by_agent_at.isoformat()
                if self.last_used_by_agent_at else None
            ),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

        if include_tokens:
            data["encrypted_access_token"] = self.encrypted_access_token
            data["encrypted_refresh_token"] = self.encrypted_refresh_token

        return data

    @classmethod
    def from_dict(
            cls,
            data: Dict[str, Any],
            encrypted_access_token: str,
            encrypted_refresh_token: Optional[str] = None
    ) -> 'OAuthToken':
        """
        Create OAuthToken instance from dictionary

        Args:
            data: Dictionary with token data
            encrypted_access_token: Pre-encrypted access token
            encrypted_refresh_token: Optional pre-encrypted refresh token

        Returns:
            New OAuthToken instance
        """
        scopes_str = None
        if 'scopes' in data and data['scopes']:
            if isinstance(data['scopes'], list):
                scopes_str = ','.join(data['scopes'])
            else:
                scopes_str = data['scopes']

        return cls(
            gateway_user_id=data['gateway_user_id'],
            idp_user_id=data['idp_user_id'],
            mcp_server_id=data['mcp_server_id'],
            idp_id=data['idp_id'],
            encrypted_access_token=encrypted_access_token,
            encrypted_refresh_token=encrypted_refresh_token,
            token_type=data.get('token_type', 'Bearer'),
            expires_at=data['expires_at'],
            scopes=scopes_str
        )

    def validate(self) -> List[str]:
        """
        Validate token configuration

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if not self.gateway_user_id or not self.gateway_user_id.strip():
            errors.append("Gateway User ID is required")

        if not self.idp_user_id or not self.idp_user_id.strip():
            errors.append("IdP User ID is required")

        if not self.mcp_server_id or not self.mcp_server_id.strip():
            errors.append("MCP server ID is required")

        if not self.idp_id or not self.idp_id.strip():
            errors.append("IdP ID is required")

        if not self.encrypted_access_token:
            errors.append("Access token is required")

        if not self.expires_at:
            errors.append("Expiration time is required")

        return errors

    def is_valid(self) -> bool:
        """
        Check if token configuration is valid

        Returns:
            True if valid, False otherwise
        """
        return len(self.validate()) == 0