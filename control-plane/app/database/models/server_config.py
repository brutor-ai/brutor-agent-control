# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Server Config Model
Links MCP servers to OAuth app clients for user portal access with multi-group scoping
Supports both OAuth-protected servers and non-OAuth servers
"""

from sqlalchemy import Column, String, Boolean, Integer, DateTime, Text, ForeignKey, Index, UniqueConstraint, Table
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, Mapped
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from app.database.models.resource_mixins import ResourceAuthorshipMixin
from app.database import Base
from app.constants.oauth import GrantType
from app.constants.endpoints import EndpointUrls

if TYPE_CHECKING:
    # Relationship annotations only (`Mapped[List["X"]]`): SQLAlchemy resolves
    # the names through its registry, and importing them here at run time would
    # be a cycle.
    from app.database.models.resource_group import ResourceGroup


class ServerConfig(ResourceAuthorshipMixin, Base):
    """
    Server Config model.

    Defines how an MCP server should be accessed through the gateway,
    including which OAuth client(s) to use for OAuth-protected servers.

    MULTI-GROUP SUPPORT: Server configs can be scoped to multiple resource groups.
    When groups are assigned, only users who are members of at least one of those
    groups will see this server in the portal. If no groups are assigned, the
    server is available to all users (global).

    OAUTH SUPPORT: OAuth client is OPTIONAL. Only required for OAuth-protected servers.
    Non-OAuth servers (filesystem, memory, etc.) can be configured without an OAuth client.
    """
    __tablename__ = "server_configs"

    # Primary Key
    id = Column(String(255), primary_key=True)

    # Tenant ID
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this configuration belongs to"
    )

    # Backend reference — exactly ONE of `mcp_server_id` or
    # `virtual_server_id` must be set. Service-layer validation
    # enforces XOR; Postgres also has a CHECK constraint via migration
    # `a049_server_config_vmcp_support`.
    mcp_server_id = Column(
        String(255),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=True,
        comment="MCP server this config is for (null when virtual_server_id is set)"
    )

    # VMCP backend. The portal calls
    # /v1/proxy/vmcp/{virtual_server_id} for tool invocations on a
    # VMCP-backed config (vs /v1/proxy/mcp/{mcp_server_id} for the
    # MCP-backed case). VMCP-side tool composition + rename +
    # OAuth delegation are handled by the proxy; the operator
    # binds the VMCP to resource groups the same way they bind a
    # real MCP server.
    virtual_server_id = Column(
        String(255),
        ForeignKey("virtual_servers.id", ondelete="CASCADE"),
        nullable=True,
        comment="Virtual MCP server this config is for (null when mcp_server_id is set)"
    )

    # OAuth App Client Reference
    oauth_client_id = Column(
        String(255),
        ForeignKey("oauth_app_clients.id", ondelete="CASCADE"),
        nullable=True,
        comment="OAuth app client to use (only required for OAuth-protected MCP servers; ignored for VMCPs since the underlying MCPs carry their own OAuth context)"
    )

    # Configuration Metadata
    name = Column(
        String(255),
        nullable=False,
        comment="Friendly name for this configuration (e.g., 'GitHub Server - Engineering')"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Optional description of this configuration"
    )

    # Priority and Status
    priority = Column(
        Integer,
        nullable=False,
        default=100,
        comment="Priority for selection (lower = higher priority)"
    )

    is_default = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this is the default config for this server"
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this configuration is enabled"
    )

    # User Portal Customization
    display_name = Column(
        String(255),
        nullable=True,
        comment="Custom display name for user portal (overrides server name)"
    )

    display_description = Column(
        Text,
        nullable=True,
        comment="Custom description for user portal (overrides server description)"
    )

    icon_url = Column(
        String(512),
        nullable=True,
        comment="Custom icon URL for user portal (overrides server icon)"
    )

    # Usage Tracking
    usage_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of times this config has been used"
    )

    last_used = Column(
        DateTime,
        nullable=True,
        comment="When this config was last used"
    )

    # Audit Fields
    # created_by / updated_by come from ResourceAuthorshipMixin — one
    # definition, stamped automatically on flush.

    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="When this config was created"
    )

    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="When this config was last updated"
    )

    # Relationships
    mcp_server = relationship(
        "MCPServer",
        back_populates="server_configs")

    # VMCP backend. The VirtualServer model doesn't
    # have a `back_populates` for server_configs today (would
    # require an explicit ORM-side change there) — using a
    # uni-directional relationship is sufficient for everything
    # the gateway does. Queries that need "all configs for VMCP
    # X" filter on `virtual_server_id` directly.
    virtual_server = relationship(
        "VirtualServer",
        foreign_keys=[virtual_server_id],
        lazy="selectin",
    )

    oauth_client = relationship(
        "OAuthAppClient",
        back_populates="server_configs")  # Can be None

    resource_groups: Mapped[List["ResourceGroup"]] = relationship(
        "ResourceGroup",
        secondary="resource_group_server_configs",  # ← Table in resource_group.py
        back_populates="server_configs",  # ← Name in ResourceGroup
        lazy="selectin"
    )

    # Indexes
    __table_args__ = (
        # Index for tenant lookups
        Index('ix_server_configs_tenant', 'tenant_id'),
        # Index for server lookups
        Index('ix_server_configs_server', 'mcp_server_id'),
        # VMCP lookup pattern
        Index('ix_server_configs_virtual_server', 'virtual_server_id'),
        # Index for OAuth client lookups (can be NULL)
        Index('ix_server_configs_oauth_client', 'oauth_client_id'),
        # Index for enabled configs
        Index('ix_server_configs_enabled', 'enabled'),
        # Index for default configs
        Index('ix_server_configs_default', 'is_default'),
        # Index for priority-based ordering
        Index('ix_server_configs_priority', 'priority'),
        # Composite unique constraint (oauth_client_id can be NULL)
        # For OAuth servers: unique per (tenant, server, oauth_client)
        # For non-OAuth servers: unique per (tenant, server, name)
        UniqueConstraint('tenant_id', 'mcp_server_id', 'oauth_client_id', 'name',
                        name='uq_server_config_tenant_server_client_name'),
    )

    # ============================================================================
    # Backend-type helpers
    # ============================================================================

    @property
    def server_kind(self) -> str:
        """Returns 'mcp' if this config points at a real MCP server,
        'vmcp' if it points at a Virtual MCP. Used by the admin UI
        for the sky-blue badge + portal for proxy URL selection.
        """
        return "vmcp" if self.virtual_server_id else "mcp"

    def get_backend_id(self) -> Optional[str]:
        """The id of the backend this config points at, regardless
        of kind. Useful for building proxy URLs."""
        return self.virtual_server_id or self.mcp_server_id

    def get_backend_name(self) -> str:
        """Display-friendly name of the backend (real MCP or VMCP)."""
        if self.virtual_server_id and self.virtual_server:
            return self.virtual_server.name
        if self.mcp_server:
            return self.mcp_server.name
        return self.name

    def __repr__(self):
        oauth_info = f", oauth_client={self.oauth_client_id}" if self.oauth_client_id else ", no OAuth"
        return f"<ServerConfig(id={self.id}, name={self.name}{oauth_info}, tenant={self.tenant_id})>"

    # ============================================================================
    # Multi-Group Access Control Methods
    # ============================================================================

    def get_group_ids(self) -> List[str]:
        """Get list of group IDs this config is assigned to"""
        return [g.id for g in self.resource_groups]

    def is_accessible_to_resource_groups(self, resource_group_ids: List[str]) -> bool:
        """Check if config is accessible to given resource groups"""
        if not resource_group_ids:
            return False

        config_groups = self.resource_groups
        if not config_groups:
            return True  # Global config

        config_group_ids = {g.id for g in config_groups}
        return bool(set(resource_group_ids) & config_group_ids)

    def is_global(self) -> bool:
        """Check if this config is globally accessible (no group restrictions)"""
        return not self.resource_groups or len(self.resource_groups) == 0

    def is_accessible_by_user(self, resource_group_ids: List[str]) -> bool:
        """Alias for is_accessible_to_resource_groups for consistency"""
        return self.is_accessible_to_resource_groups(resource_group_ids)

    def get_display_name(self) -> str:
        """Display name — config-side override wins; otherwise the
        backend (MCP server or VMCP) name; otherwise the config name.
        """
        if self.display_name:
            return self.display_name
        if self.virtual_server_id and self.virtual_server is not None:
            return self.virtual_server.name
        if self.mcp_server:
            return self.mcp_server.name
        return self.name

    def get_display_description(self) -> Optional[str]:
        """Display description — config-side override > backend
        description > config description. Works for both MCP and
        VMCP backends.
        """
        if self.display_description:
            return self.display_description
        if self.virtual_server_id and self.virtual_server is not None:
            return self.virtual_server.description
        if self.mcp_server:
            return self.mcp_server.description
        return self.description

    def get_icon_url(self) -> Optional[str]:
        """Icon URL — config-side override > backend icon (MCP
        only; VMCPs don't have their own icons today, so VMCP-backed
        configs fall back to the config's explicit icon_url or
        none).
        """
        # Priority 1: Config's explicit icon_url
        if self.icon_url:
            return self.icon_url

        # Priority 2: MCP server's database icon. VMCPs have no
        # icon storage today — admin UI renders the sky-blue VMCP
        # badge instead.
        if self.virtual_server_id is None and self.mcp_server and self.mcp_server.has_icon():
            return f"{EndpointUrls.MCP_SERVER_ADMIN.value}/{self.mcp_server.id}/icon"

        return None

    # ============================================================================
    # OAuth Detection Methods
    # ============================================================================

    def requires_oauth(self) -> bool:
        """Check if this server configuration requires OAuth authentication.

        VMCP-backed configs always return False — VMCPs delegate
        to their underlying MCP servers, each of which carries its
        own OAuth context. The config-level oauth_client_id is
        meaningless for VMCPs.
        """
        # VMCP-backed configs delegate auth per-call
        # to the underlying MCP. No OAuth at the VMCP scope.
        if self.virtual_server_id is not None:
            return False

        # Check if OAuth client is configured
        if self.oauth_client_id:
            return True

        # Check server's auth_type
        if self.mcp_server and self.mcp_server.auth_type:
            auth_type = self.mcp_server.auth_type.lower()
            return auth_type in ['oauth', 'oauth2']

        return False

    def is_open_server(self) -> bool:
        """Check if this is a non-OAuth server (open access)"""
        return not self.requires_oauth()

    # ============================================================================
    # Serialization Methods
    # ============================================================================

    def to_dict(self, include_relationships: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_relationships: Whether to include full mcp_server, oauth_client, and groups data

        Returns:
            Dictionary representation
        """
        # `effective_enabled` considers the
        # underlying backend's `enabled` flag whether it's an MCP
        # server or a VMCP (admins disabling a VMCP cascades the
        # same way it does for a real MCP).
        backend_enabled = True
        if self.virtual_server_id and self.virtual_server is not None:
            backend_enabled = bool(self.virtual_server.enabled)
        elif self.mcp_server is not None:
            backend_enabled = bool(self.mcp_server.enabled)

        data = {
            'id': self.id,
            # Both backend IDs surfaced. Admin UI picks
            # one depending on `server_kind`; portal builds the
            # proxy URL the same way.
            'mcp_server_id': self.mcp_server_id,
            'virtual_server_id': self.virtual_server_id,
            'server_kind': self.server_kind,  # 'mcp' | 'vmcp'
            'oauth_client_id': self.oauth_client_id,
            'group_ids': self.get_group_ids(),  # NEW: Array of group IDs
            'name': self.name,
            'description': self.description,
            'priority': self.priority,
            'is_default': self.is_default,
            'enabled': self.enabled,
            'effective_enabled': self.enabled and backend_enabled,
            'display_name': self.display_name,
            'display_description': self.display_description,
            'icon_url': self.icon_url,
            'usage_count': self.usage_count,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            # AI Resource authorship block. No `version`: that belongs
            # to the MCP server this configures, not to the config.
            **self.authorship_dict()
        }

        if include_relationships:
            # Include MCP Server info
            if self.mcp_server:
                icon_info = None
                if self.mcp_server.has_icon():
                    icon_info = {
                        'url': f"{EndpointUrls.MCP_SERVER_ADMIN.value}/{self.mcp_server.id}/icon"
                    }

                data['mcp_server'] = {
                    'id': self.mcp_server.id,
                    'name': self.mcp_server.name,
                    'description': self.mcp_server.description,
                    'base_url': self.mcp_server.base_url,
                    'mcp_endpoint_path': self.mcp_server.mcp_endpoint_path,
                    'auth_type': self.mcp_server.auth_type,
                    'timeout': self.mcp_server.timeout,
                    'enabled': self.mcp_server.enabled,
                    'icon': icon_info
                }

            # Include VMCP info when this config is
            # VMCP-backed. The admin UI uses this to populate the
            # form when editing + show the backend name in the
            # list view.
            if self.virtual_server:
                data['virtual_server'] = {
                    'id': self.virtual_server.id,
                    'name': self.virtual_server.name,
                    'description': self.virtual_server.description,
                    'enabled': self.virtual_server.enabled,
                    # Operators want to see "how many tools" at a
                    # glance — the VMCP capabilities JSON carries
                    # the tool count, no separate query needed.
                    'tool_count': len(
                        (self.virtual_server.capabilities or {}).get("tools", [])
                    ),
                }

            # Include OAuth Client info
            if self.oauth_client:
                # Convert scopes list to space-separated string for API response
                scopes_list = self.oauth_client.get_scopes_list() if hasattr(self.oauth_client, 'get_scopes_list') else (
                    self.oauth_client.scopes or [])
                scopes_string = ' '.join(scopes_list) if isinstance(scopes_list, list) else str(scopes_list)

                # Determine OAuth flow based on client configuration
                flow = GrantType.AUTHORIZATION_CODE_PKCE.value
                if self.oauth_client.client_type == 'confidential':
                    flow = GrantType.AUTHORIZATION_CODE.value

                data['oauth_client'] = {
                    'id': self.oauth_client.id,
                    'name': self.oauth_client.name,
                    'app_type': self.oauth_client.app_type,
                    'client_id': self.oauth_client.client_id,
                    'client_type': self.oauth_client.client_type,
                    'flow': flow,
                    'redirect_uri': self.oauth_client.redirect_uri,
                    'scopes': scopes_string,
                    'supports_pkce': self.oauth_client.supports_pkce,
                    'idp_id': self.oauth_client.idp_id,
                    'is_active': self.oauth_client.is_active
                }

            # Include Groups info
            if self.resource_groups:
                data['groups'] = [
                    {
                        'id': group.id,
                        'name': group.name,
                        'display_name': group.display_name,
                        'group_type': group.group_type,
                        'is_active': group.is_active,
                        'parent_group_id': group.parent_group_id
                    }
                    for group in self.resource_groups
                ]
            else:
                data['groups'] = []  # Empty array for global configs

        return data

    def to_user_portal_dict(self, base_url: str) -> Dict[str, Any]:
        """
        Convert server config to user portal format with OAuth client details
        and proxied endpoints for gateway MCP and OAuth proxy
        """
        result = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mcp_server_id": self.mcp_server_id,
            "mcp_server_name": self.mcp_server.name if self.mcp_server else None,
            "oauth_client_id": self.oauth_client_id,
            "oauth_required": self.requires_oauth(),
            "is_open_server": self.is_open_server(),
            # Effective enabled: ServerConfig.enabled AND MCPServer.enabled
            # If MCPServer is disabled, this config is effectively disabled
            "enabled": self.enabled and (self.mcp_server.enabled if self.mcp_server else True),
            "display_name": self.display_name,
            "display_description": self.display_description,
            "icon_url": self.icon_url,
            "priority": self.priority,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        # Add MCP server details if available
        if self.mcp_server:
            mcp_server_dict = {
                "id": self.mcp_server.id,
                "name": self.mcp_server.name,
                "base_url": f"{base_url}{EndpointUrls.GATEWAY_MCP_PROXY.value}/{self.mcp_server.id}",
                "mcp_endpoint_path": "",
                "auth_type": self.mcp_server.auth_type,
                "timeout": self.mcp_server.timeout
            }

            # Add icon information if available
            if self.mcp_server.has_icon():
                mcp_server_dict["icon"] = {
                    "url": f"{EndpointUrls.MCP_SERVER_ADMIN.value}/{self.mcp_server.id}/icon",
                    "mime_type": self.mcp_server.icon_mime_type,
                    "size": self.mcp_server.icon_size,
                    "uploaded_at": self.mcp_server.icon_uploaded_at.isoformat() if self.mcp_server.icon_uploaded_at else None
                }

            result["mcp_server"] = mcp_server_dict

        # Add OAuth client details if OAuth is required
        if self.requires_oauth() and self.oauth_client:
            oauth_client = self.oauth_client

            # Determine the flow based on client configuration
            flow = GrantType.AUTHORIZATION_CODE_PKCE.value
            if oauth_client.client_type == "confidential":
                flow = GrantType.AUTHORIZATION_CODE.value

            result["oauth_client"] = {
                "id": oauth_client.id,
                "name": oauth_client.name,
                "app_type": oauth_client.app_type,
                "client_id": oauth_client.client_id,
                "client_type": oauth_client.client_type,
                "flow": flow,
                "redirect_uri": oauth_client.redirect_uri,
                "scopes": oauth_client.scopes,
                "supports_pkce": oauth_client.supports_pkce,
                "idp_id": oauth_client.idp_id,
            }

            # Add Identity Provider details if available
            if oauth_client.idp:
                idp = oauth_client.idp
                result["oauth_client"]["idp"] = {
                    "id": idp.id,
                    "name": idp.name,
                    "type": idp.type,
                    "issuer": idp.issuer,
                    "authorization_endpoint": idp.authorization_endpoint,
                    "token_endpoint": idp.token_endpoint,
                    "userinfo_endpoint": idp.userinfo_endpoint,
                    "jwks_uri": idp.jwks_uri,
                    "supports_pkce": idp.supports_pkce,
                    "supports_refresh_tokens": idp.supports_refresh_tokens,
                }

            # Add rewritten endpoints for gateway proxy
            # These should point to your gateway's proxy endpoints
            # OAuth proxy token exchange endpoint: "/v1/proxy/{mcp_server_id}/{idp_server_id}/token"
            # OAuth Open ID connect discovery endpoint: "/v1/proxy/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration"
            result["oauth_client"]["token_endpoint"] = \
                f"{base_url}{EndpointUrls.GATEWAY_MCP_PROXY.value}/{self.mcp_server.id}/{oauth_client.idp_id}/token"
            result["oauth_client"]["openid_config_endpoint"] = \
                f"{base_url}{EndpointUrls.GATEWAY_MCP_PROXY.value}/{self.mcp_server.id}/{oauth_client.idp_id}/.well-known/openid-configuration"

        result["user_oauth_status"] = {
            "has_valid_token": False,
            "needs_authorization": True,
            "token_expires_at": None
        }

        return result

    def increment_usage(self):
        """Increment usage counter and update last_used timestamp"""
        self.usage_count += 1
        self.last_used = func.now()