# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Server Config API Models
Pydantic models for server configuration requests and responses with multi-group support
Supports both OAuth-protected servers and non-OAuth servers
"""

from datetime import datetime
from pydantic import BaseModel, Field, validator, ConfigDict
from typing import Optional, List, Dict, Any
from app.constants.oauth import ClientType, GrantType
from app.api.v1.models.admin.resource_fields import ResourceAuthorshipFields

# ============================================================================
# Admin Models
# ============================================================================

class ServerConfigCreate(BaseModel):
    """Schema for creating a new server configuration.

    Exactly ONE of `mcp_server_id` or `virtual_server_id` must be set.
    """
    mcp_server_id: Optional[str] = Field(
        None,
        description=(
            "MCP server ID. Required when the config is MCP-backed; "
            "leave null when virtual_server_id is set."
        ),
    )
    virtual_server_id: Optional[str] = Field(
        None,
        description=(
            "Virtual MCP server ID. Mutually exclusive with "
            "mcp_server_id. When set, the portal calls "
            "/v1/proxy/vmcp/{virtual_server_id} for tool invocations."
        ),
    )
    oauth_client_id: Optional[str] = Field(
        None,
        description=(
            "OAuth app client ID (required for OAuth-protected MCP "
            "servers; ignored for VMCP-backed configs since VMCPs "
            "delegate per-call auth to the underlying MCPs)."
        ),
    )
    group_ids: Optional[List[str]] = Field(None, description="Resource group IDs (empty/null = available to all users)")
    name: str = Field(..., description="Configuration name", min_length=1, max_length=255)
    description: Optional[str] = Field(None, description="Optional description")
    priority: int = Field(100, description="Priority for selection (lower = higher priority)", ge=1)
    is_default: bool = Field(False, description="Whether this is the default config")
    enabled: bool = Field(True, description="Whether this config is enabled")
    display_name: Optional[str] = Field(None, description="Display name for user portal", max_length=255)
    display_description: Optional[str] = Field(None, description="Display description for user portal")
    icon_url: Optional[str] = Field(None, description="Icon URL for user portal")

    @validator('group_ids')
    def validate_group_ids(cls, v):
        """Ensure group_ids is a list if provided"""
        if v is not None and not isinstance(v, list):
            raise ValueError("group_ids must be a list")
        return v

    @validator("virtual_server_id", always=True)
    def validate_backend_xor(cls, v, values):
        """Exactly one of mcp_server_id / virtual_server_id must be set.

        Pydantic v1 quirk: `always=True` makes this validator run even
        when the field is None, which is what we need to catch the
        "both null" case. The `values` dict carries already-validated
        fields — `mcp_server_id` is declared first so it's present.
        """
        mcp_id = values.get("mcp_server_id")
        if v and mcp_id:
            raise ValueError(
                "Set exactly one of mcp_server_id or virtual_server_id, not both."
            )
        if not v and not mcp_id:
            raise ValueError(
                "Must set one of mcp_server_id or virtual_server_id."
            )
        return v


class ServerConfigUpdate(BaseModel):
    """Schema for updating a server configuration"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    group_ids: Optional[List[str]] = None  # Allow changing group assignments
    priority: Optional[int] = Field(None, ge=1)
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=255)
    display_description: Optional[str] = None
    icon_url: Optional[str] = None

    @validator('group_ids')
    def validate_group_ids(cls, v):
        """Ensure group_ids is a list if provided"""
        if v is not None and not isinstance(v, list):
            raise ValueError("group_ids must be a list")
        return v


class ResourceGroupInfo(BaseModel):
    """Nested user group information"""
    id: str
    name: str
    display_name: Optional[str] = None
    group_type: Optional[str] = None
    is_active: bool
    parent_group_id: Optional[str] = None


class MCPServerInfo(BaseModel):
    """Nested MCP server information"""
    id: str
    name: str
    description: Optional[str] = None
    base_url: str
    mcp_endpoint_path: str
    auth_type: str
    timeout: Optional[int] = None
    enabled: bool = True
    # MCP Apps governance: when False, the gateway suppresses
    # `_meta.ui.resourceUri` for this server (the portal won't even
    # try to render the iframe). Default True. Surfacing here so
    # admin / portal UIs can show the toggle and its effective state.
    mcp_apps_enabled: bool = True
    idp_id: Optional[str] = None
    icon: Optional[Dict[str, Any]] = None  # Icon information
    capabilities: Optional[Dict[str, Any]] = None  # Discovered capabilities
    discovered_at: Optional[str] = None  # When capabilities were discovered
    # Streamable-HTTP listener affinity. The portal's MCP
    # client reads this to decide whether to open the optional
    # SSE listener stream — when False, it skips the stream
    # entirely, avoiding the reconnect loop that pure
    # request/response servers (e.g. AWS Knowledge MCP)
    # otherwise produce. Default True for back-compat: legacy
    # rows that haven't been re-probed behave as before.
    wants_listener: bool = True
    health_status: Optional[Dict[str, Any]] = None  # Health check status


class UserOAuthStatus(BaseModel):
    """User's OAuth status for this server"""
    has_valid_token: bool
    needs_authorization: bool
    token_expires_at: Optional[datetime] = None


class IdPInfo(BaseModel):
    """Identity Provider metadata"""
    id: str = Field(..., description="IdP ID")
    name: str = Field(..., description="IdP name")
    type: str = Field(..., description="IdP type (e.g., oidc_discoverable, manual, github, google)")
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    supports_pkce: bool = Field(..., description="Whether IdP supports PKCE")
    supports_refresh_tokens: bool = Field(..., description="Whether IdP supports refresh tokens")


class OAuthClientInfo(BaseModel):
    """Nested OAuth client information"""
    id: str
    name: str
    app_type: str
    client_id: str
    client_type: str
    flow: str
    redirect_uri: str
    scopes: str
    supports_pkce: bool
    idp_id: str
    idp: Optional[IdPInfo] = None
    token_endpoint: Optional[str] = None
    openid_config_endpoint: Optional[str] = None


class VirtualServerInfo(BaseModel):
    """Nested VMCP info for VMCP-backed server configs."""
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool = True
    tool_count: int = Field(0, description="Number of tools composed in this VMCP")


class ServerConfigResponse(ResourceAuthorshipFields):
    """Schema for server configuration response.

    `mcp_server_id` is optional: VMCP-backed configs leave it null. `server_kind` distinguishes
    the two backends ('mcp' or 'vmcp') for admin-UI rendering.
    """
    id: str
    mcp_server_id: Optional[str] = None
    virtual_server_id: Optional[str] = None
    server_kind: str = Field(
        "mcp",
        description="'mcp' for a real-MCP-backed config, 'vmcp' for a Virtual MCP",
    )
    oauth_client_id: Optional[str] = None
    name: str
    description: Optional[str]
    priority: int
    is_default: bool
    enabled: bool
    display_name: Optional[str]
    display_description: Optional[str]
    icon_url: Optional[str]
    usage_count: int
    last_used: Optional[str]
    # created_by / updated_by come from ResourceAuthorshipFields.
    created_at: str
    updated_at: str
    # Multi-group support
    group_ids: Optional[List[str]] = Field(default_factory=list, description="Resource group IDs this config is assigned to")
    groups: Optional[List[ResourceGroupInfo]] = Field(None, description="Full group information when include_relationships=True")
    # OAuth status fields
    requires_oauth: bool = Field(False, description="Whether this server requires OAuth authentication")
    is_open_server: bool = Field(True, description="Whether this is a non-OAuth server")
    # Relationships
    mcp_server: Optional[MCPServerInfo] = None
    virtual_server: Optional[VirtualServerInfo] = None
    oauth_client: Optional[OAuthClientInfo] = None

    class Config:
        from_attributes = True


class ServerConfigListResponse(BaseModel):
    """Schema for list of server configurations"""
    configs: List[Dict[str, Any]]
    total: int


class ServerConfigStatsResponse(BaseModel):
    """Schema for server config statistics"""
    total_configs: int
    enabled_configs: int
    default_configs: int
    total_usage: int
    active_last_30_days: int
    never_used: int
    configs_with_groups: int = Field(0, description="Number of configs with at least one group")
    global_configs: int = Field(0, description="Number of configs with no groups (global)")
    oauth_configs: int = Field(0, description="Number of OAuth-protected configs")
    open_configs: int = Field(0, description="Number of non-OAuth configs")


# ============================================================================
# User Portal Models
# ============================================================================

class OAuthStatusInfo(BaseModel):
    """User's OAuth status for a server"""
    has_valid_token: bool = Field(..., description="Whether user has a valid OAuth token")
    needs_authorization: bool = Field(..., description="Whether user needs to authorize")
    token_expires_at: Optional[str] = Field(None, description="When the current token expires")


class OAuthEndpointsInfo(BaseModel):
    """OAuth/OIDC endpoints for authorization"""
    authorization_endpoint: Optional[str] = Field(None, description="OAuth authorization endpoint URL")
    token_endpoint: Optional[str] = Field(None, description="OAuth token endpoint URL")
    userinfo_endpoint: Optional[str] = Field(None, description="OIDC userinfo endpoint URL")
    revocation_endpoint: Optional[str] = Field(None, description="OAuth token revocation endpoint URL")
    jwks_uri: Optional[str] = Field(None, description="JWKS (JSON Web Key Set) URI")
    issuer: Optional[str] = Field(None, description="OAuth/OIDC issuer URL")


class ServerIconInfo(BaseModel):
    """Server icon information"""
    type: str = Field(..., description="Icon type: 'file' or 'url'")
    mime_type: Optional[str] = Field(None, description="MIME type for file-based icons")
    size: Optional[int] = Field(None, description="File size in bytes for file-based icons")
    hash: Optional[str] = Field(None, description="SHA256 hash of icon data")
    uploaded_at: Optional[str] = Field(None, description="When icon was uploaded")
    url: str = Field(..., description="URL to fetch the icon")


class ServerCapabilitiesInfo(BaseModel):
    """Server capabilities information"""
    tools: Optional[List[str]] = Field(None, description="Available tools")
    resources: Optional[List[str]] = Field(None, description="Available resources")
    prompts: Optional[List[str]] = Field(None, description="Available prompts")


class UserPortalServerConfigResponse(BaseModel):
    """
    Server configuration response for user portal

    Includes:
    - Server configuration details
    - MCP server information
    - OAuth client with rewritten endpoints (if OAuth required)
    - User's OAuth status
    """
    id: str
    name: str
    description: Optional[str] = None
    mcp_server_id: str
    mcp_server_name: str
    enabled: bool
    display_name: Optional[str] = None
    display_description: Optional[str] = None
    icon_url: Optional[str] = None
    priority: int
    oauth_required: bool
    is_open_server: bool
    is_default: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Related entities
    mcp_server: MCPServerInfo
    oauth_client_id: Optional[str] = None
    oauth_client: Optional[OAuthClientInfo] = None

    # User-specific status (only relevant for OAuth servers)
    user_oauth_status: Optional[UserOAuthStatus] = None

    class Config:
        from_attributes = True


class UserPortalServerListItem(BaseModel):
    """Schema for server config in user portal list"""
    id: str
    mcp_server_id: str
    name: str
    description: Optional[str]
    icon_url: Optional[str]
    oauth_required: bool
    is_open_server: bool
    user_oauth_status: Optional[OAuthStatusInfo] = None
    oauth_endpoints: Optional[OAuthEndpointsInfo] = Field(
        None,
        description="OAuth/OIDC endpoints for authorization (only for OAuth servers)"
    )
    idp_info: Optional[IdPInfo] = Field(
        None,
        description="Identity Provider metadata (only for OAuth servers)"
    )


class UserPortalServerConfigListResponse(BaseModel):
    """List of server configurations available to user"""
    servers: List[UserPortalServerConfigResponse]
    total: int


# ============================================================================
# OAuth App Client Models (for completeness)
# ============================================================================

class OAuthAppClientCreate(BaseModel):
    """Schema for creating a new OAuth app client"""
    name: str = Field(..., description="Friendly name", min_length=1, max_length=255)
    app_type: str = Field(..., description="Application type: webapp, mobile, desktop, cli, service")
    idp_id: str = Field(..., description="Identity Provider ID")
    client_id: str = Field(..., description="OAuth client ID from IdP", min_length=1)
    client_secret: Optional[str] = Field(None, description="OAuth client secret (for confidential clients)")
    redirect_uri: str = Field(..., description="OAuth redirect URI")
    scopes: List[str] = Field(default_factory=list, description="Default OAuth scopes")
    client_type: str = Field(..., description="Client type: confidential or public")
    supports_pkce: bool = Field(True, description="Whether this client uses PKCE")
    allowed_grant_types: List[str] = Field(
        default_factory=lambda: [GrantType.AUTHORIZATION_CODE.value],
        description="Allowed OAuth grant types"
    )
    description: Optional[str] = Field(None, description="Optional description")

    @validator('app_type')
    def validate_app_type(cls, v):
        valid_types = ['webapp', 'mobile', 'desktop', 'cli', 'service']
        if v not in valid_types:
            raise ValueError(f"app_type must be one of: {', '.join(valid_types)}")
        return v

    @validator('client_type')
    def validate_client_type(cls, v):
        if v not in ClientType:
            valid_types = ", ".join([ct.value for ct in ClientType])
            raise ValueError(f"client_type must be one of: {valid_types}")
        return v


class OAuthAppClientUpdate(BaseModel):
    """Schema for updating an OAuth app client"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    client_id: Optional[str] = Field(None, min_length=1)
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None
    scopes: Optional[List[str]] = None
    supports_pkce: Optional[bool] = None
    allowed_grant_types: Optional[List[str]] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


class OAuthAppClientResponse(BaseModel):
    """Schema for OAuth app client response (excludes secret)"""
    id: str
    name: str
    app_type: str
    idp_id: str
    client_id: str
    redirect_uri: str
    scopes: List[str]
    client_type: str
    supports_pkce: bool
    allowed_grant_types: List[str]
    description: Optional[str]
    registered_at: str
    registered_by: Optional[str]
    last_used: Optional[str]
    is_active: bool
    auto_registered: bool = False
    registration_client_uri: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class OAuthAppClientListResponse(BaseModel):
    """Schema for list of OAuth app clients"""
    clients: List[OAuthAppClientResponse]
    total: int


class AutoRegisterOAuthAppClientRequest(BaseModel):
    """Request model for auto-registering an OAuth client via RFC 7591 Dynamic Client Registration"""
    idp_id: str = Field(..., description="Identity Provider ID (must support DCR)")
    client_name: str = Field(..., description="Human-readable name for the client", min_length=1, max_length=255)
    app_type: str = Field("webapp", description="Application type: webapp, mobile, desktop, cli, service")
    redirect_uri: str = Field(..., description="OAuth redirect URI")
    scopes: List[str] = Field(default_factory=list, description="Requested OAuth scopes")
    token_endpoint_auth_method: str = Field("client_secret_post", description="Token endpoint auth method")
    grant_types: List[str] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"],
        description="OAuth grant types"
    )
    response_types: List[str] = Field(
        default_factory=lambda: ["code"],
        description="OAuth response types"
    )
    description: Optional[str] = Field(None, description="Optional description", max_length=500)