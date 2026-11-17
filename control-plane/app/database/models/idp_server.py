# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity Provider (IdP) Server Configuration Model
Stores OAuth/OIDC identity provider configurations
"""

from datetime import datetime, timezone
from typing import Dict, Any

from sqlalchemy import Column, String, Boolean, Integer, Text, JSON, DateTime, Index, ForeignKey
from sqlalchemy.sql import func

from app.database import Base


class IdPServer(Base):
    """
    Identity Provider server configuration for OAuth/OIDC authentication

    Supports both OIDC-discoverable (/.well-known/openid-configuration) providers (Keycloak, Auth0 - Okta, Google)
    and manual configurations of endpoints (GitHub OAuth, custom providers).
    """
    __tablename__ = "idp_servers"

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
    issuer = Column(String(500), nullable=False)
    type = Column(String(50), nullable=False)  # oidc_discoverable, manual, github, google

    # Discovery
    well_known_url = Column(String(500), nullable=True)  # For OIDC discovery

    # Manual endpoints (for non-OIDC or override)
    authorization_endpoint = Column(String(500), nullable=True)
    token_endpoint = Column(String(500), nullable=True)
    revocation_endpoint = Column(String(500), nullable=True)
    introspection_endpoint = Column(String(500), nullable=True)
    jwks_uri = Column(String(500), nullable=True)
    userinfo_endpoint = Column(String(500), nullable=True)
    registration_endpoint = Column(String(500), nullable=True, comment="RFC 7591 Dynamic Client Registration endpoint")

    # Capabilities
    supports_pkce = Column(Boolean, nullable=False, default=True)
    supports_refresh_tokens = Column(Boolean, nullable=False, default=True)
    supports_client_credentials = Column(Boolean, nullable=False, default=False)
    token_endpoint_auth_methods = Column(JSON, nullable=False, default=lambda: [])  # ["client_secret_post", ...]

    # Metadata
    scopes_supported = Column(JSON, nullable=False, default=lambda: [])
    response_types_supported = Column(JSON, nullable=False, default=lambda: [])
    grant_types_supported = Column(JSON, nullable=False, default=lambda: [])

    # Cache
    cached_config = Column(JSON, nullable=True)  # Full OIDC discovery response
    last_discovered = Column(DateTime(timezone=True), nullable=True)
    discovery_cache_ttl = Column(Integer, nullable=False, default=3600)  # Seconds

    # Status
    enabled = Column(Boolean, nullable=False, default=True)
    verified = Column(Boolean, nullable=False, default=False)  # Manually verified by admin

    # Additional metadata
    description = Column(Text, nullable=True)
    logo_url = Column(String(500), nullable=True)

    # ==========================================================================
    # The protocol, and the SAML columns. Only 'oidc' is offered in this
    # release (`app/composition/identity.py`): nothing writes the `saml_*`
    # columns, `trust_saml_email_unverified`, `target_user_type` or
    # `saml_group_to_end_user_group_mapping`, and no response shows them.
    # They stay in the schema until the migrations are squashed.
    # ==========================================================================
    protocol = Column(
        String(20), nullable=True, default="oidc",
        comment="Identity protocol: 'oidc' (default) or 'saml'",
    )
    saml_metadata_url = Column(Text, nullable=True)
    saml_metadata_xml = Column(Text, nullable=True)
    saml_idp_entity_id = Column(Text, nullable=True)
    saml_idp_sso_url = Column(Text, nullable=True)
    saml_idp_certificate = Column(Text, nullable=True)
    saml_sp_entity_id = Column(Text, nullable=True)
    saml_attribute_mapping = Column(JSON, nullable=True)
    saml_group_attribute = Column(String(255), nullable=True)
    saml_group_to_role_mapping = Column(JSON, nullable=True)
    saml_force_authn = Column(Boolean, nullable=True, default=False)
    saml_want_assertions_signed = Column(Boolean, nullable=True, default=True)
    trust_saml_email_unverified = Column(
        Boolean,
        nullable=False,
        server_default="false",
        default=False,
        comment=(
            "When TRUE, the KB OIDC-derivation hook trusts the email "
            "claim from this IdP as verified, even though SAML doesn't "
            "carry an explicit email_verified flag. Set by tenant admin "
            "during IdP registration."
        ),
    )
    saml_last_metadata_refresh = Column(DateTime(timezone=True), nullable=True)

    # The core verifies workload-identity (agent) tokens
    # from this IdP against its JWKS and requires `aud` to be one of these.
    workload_audiences = Column(
        JSON,
        nullable=True,
        comment=(
            "Accepted `aud` values for workload-identity tokens from this IdP "
            "Empty or null: this IdP authenticates no "
            "workloads (fail closed)."
        ),
    )

    target_user_type = Column(
        String(20),
        nullable=True,
        default='end_user',
        comment=(
            "'end_user' (default) or 'tenant_admin'. Discriminates "
            "which user table SAML JIT writes to. One IdP entry = "
            "one audience; create two IdP entries for dual-audience "
            "tenants."
        ),
    )

    saml_group_to_end_user_group_mapping = Column(
        JSON,
        nullable=True,
        comment=(
            "IdP-group → EndUserGroup-name mapping for resource "
            "access. Only used when target_user_type='end_user'. "
            "Example: {\"okta-engineers\": \"engineering-team\"}."
        ),
    )

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        # Primary queries - "Get all enabled IdPs for tenant"
        Index('ix_idp_servers_tenant_enabled', 'tenant_id', 'enabled'),

        # IdP lookup by name within tenant
        Index('ix_idp_servers_tenant_name', 'tenant_id', 'name'),

        # Type-based filtering - "Get all OIDC discoverable IdPs"
        Index('ix_idp_servers_tenant_type', 'tenant_id', 'type'),
        Index('ix_idp_servers_tenant_type_enabled', 'tenant_id', 'type', 'enabled'),

        # Verified IdPs - "Get verified IdPs only"
        Index('ix_idp_servers_tenant_verified', 'tenant_id', 'verified'),

        # Discovery cache management - "Find IdPs needing refresh"
        Index('ix_idp_servers_tenant_discovery', 'tenant_id', 'last_discovered', 'type'),

        # Audit queries
        Index('ix_idp_servers_created_at', 'tenant_id', 'created_at'),

        {
            'comment': 'OAuth/OIDC Identity Provider configurations'
        }
    )

    def __repr__(self) -> str:
        return f"<IdPServer(id='{self.id}', name='{self.name}', type='{self.type}')>"

    def is_cache_valid(self) -> bool:
        """Check if cached discovery config is still valid"""
        if not self.cached_config or not self.last_discovered:
            return False

        age_seconds = (datetime.now(timezone.utc) - self.last_discovered).total_seconds()
        return age_seconds < self.discovery_cache_ttl

    def needs_discovery(self) -> bool:
        """Check if OIDC discovery should be performed"""
        if self.type not in ['oidc_discoverable', 'google']:
            return False

        return not self.is_cache_valid()

    def update_from_discovery(self, discovery_data: Dict[str, Any]) -> None:
        """Update endpoints and metadata from OIDC discovery response"""
        self.cached_config = discovery_data
        self.last_discovered = datetime.now(timezone.utc)

        # Update individual endpoints
        self.authorization_endpoint = discovery_data.get('authorization_endpoint')
        self.token_endpoint = discovery_data.get('token_endpoint')
        self.revocation_endpoint = discovery_data.get('revocation_endpoint')
        self.introspection_endpoint = discovery_data.get('introspection_endpoint')
        self.jwks_uri = discovery_data.get('jwks_uri')
        self.userinfo_endpoint = discovery_data.get('userinfo_endpoint')
        self.registration_endpoint = discovery_data.get('registration_endpoint')

        # Update supported features
        if 'scopes_supported' in discovery_data:
            self.scopes_supported = discovery_data['scopes_supported']

        if 'response_types_supported' in discovery_data:
            self.response_types_supported = discovery_data['response_types_supported']

        if 'grant_types_supported' in discovery_data:
            self.grant_types_supported = discovery_data['grant_types_supported']

        if 'token_endpoint_auth_methods_supported' in discovery_data:
            self.token_endpoint_auth_methods = discovery_data['token_endpoint_auth_methods_supported']

        # Check PKCE support
        if 'code_challenge_methods_supported' in discovery_data:
            self.supports_pkce = len(discovery_data['code_challenge_methods_supported']) > 0

    def to_dict(self, include_cached: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        data = {
            "id": self.id,
            "name": self.name,
            "issuer": self.issuer,
            "type": self.type,
            "protocol": self.protocol or "oidc",
            "well_known_url": self.well_known_url,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "introspection_endpoint": self.introspection_endpoint,
            "jwks_uri": self.jwks_uri,
            "userinfo_endpoint": self.userinfo_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "supports_pkce": self.supports_pkce,
            "supports_refresh_tokens": self.supports_refresh_tokens,
            "supports_client_credentials": self.supports_client_credentials,
            "token_endpoint_auth_methods": self.token_endpoint_auth_methods or [],
            "scopes_supported": self.scopes_supported or [],
            "response_types_supported": self.response_types_supported or [],
            "grant_types_supported": self.grant_types_supported or [],
            "last_discovered": self.last_discovered.isoformat() if self.last_discovered else None,
            "discovery_cache_ttl": self.discovery_cache_ttl,
            "enabled": self.enabled,
            "verified": self.verified,
            "description": self.description,
            "logo_url": self.logo_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            # Accepted workload-token audiences ([] = none accepted).
            "workload_audiences": self.workload_audiences or [],
        }

        if include_cached:
            data["cached_config"] = self.cached_config

        return data

    def get_oauth_config(self) -> Dict[str, str]:
        """Get essential OAuth configuration for client use"""
        return {
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "jwks_uri": self.jwks_uri,
            "userinfo_endpoint": self.userinfo_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "issuer": self.issuer
        }