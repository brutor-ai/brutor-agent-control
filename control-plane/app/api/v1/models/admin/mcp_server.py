# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server API Models
Pydantic models for MCP server management endpoints
"""

from typing import Optional, Dict, Any, List

from pydantic import BaseModel, Field, field_validator, ConfigDict
from app.api.v1.models.admin.residency import validate_region
from app.database.models.resource_group import ResourceGroup, mcp_governance_value_problem
from app.services import mcp_exec_limits
from app.api.v1.models.admin.mcp_limits_document import McpLimits
from app.api.v1.models.admin.resource_fields import ResourceMetaFields
from app.api.v1.models.admin.resource_fields import ResourceVersionField


# ============================================================================
# Request Models
# ============================================================================

def _readable_tenant_limits(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """An MCP server's `tenant_limits` is held to the rule of every MCP limits
    document (`ResourceGroup.validate_mcp_limits_structure`)."""
    ok, problem = ResourceGroup.validate_mcp_limits_structure(value)
    if not ok:
        raise ValueError(problem)
    return value


def _known_default_access(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The gateway reads an MCP server's `default_*_access` as a ceiling
    over every group (`usage::mcp_default_access_from_gov`) and an access it
    does not know as denied: such a value is refused here rather than
    stored. A `null` states nothing."""
    problem = mcp_governance_value_problem(value, null_states_nothing=True)
    if problem:
        raise ValueError(problem)
    return value


class ServerConnectionTestRequest(BaseModel):
    """Request model for testing server connections"""
    url: str = Field(..., description="Server URL to test")
    auth_type: str = Field(default="none", description="Authentication type")
    timeout: float = Field(default=10.0, description="Connection timeout in seconds")


class ServerCreateRequest(ResourceVersionField):
    """Request model for creating a new MCP server"""
    name: str = Field(..., description="Server display name")
    description: Optional[str] = Field(None, description="Server description")
    base_url: str = Field(
        ...,
        description="Server base URL (e.g., http://localhost:3000)"
    )
    mcp_endpoint_path: str = Field(
        default="",
        description="MCP endpoint path (e.g., /mcp or /api/mcp)"
    )
    auth_type: str = Field(
        default="none",
        description="Authentication type (none, bearer, oauth)"
    )
    auth_token: Optional[str] = Field(
        default=None,
        description=(
            "The static token the gateway sends to the server as a bearer. Write-only: it is stored "
            "encrypted and never returned (`has_auth_token` says whether one is set). Refused with 503 "
            "where this control plane has no encryption service (ENCRYPTION_KEY). It belongs to "
            "`auth_type` bearer: one sent with another auth type is refused, and saving a server "
            "with another auth type removes the token stored for it."
        ),
    )
    idp_id: Optional[str] = Field(None, description="Identity Provider ID for OAuth")
    timeout: float = Field(default=30.0, description="Request timeout in seconds")
    max_concurrent_requests: Optional[int] = Field(None, ge=1, description=mcp_exec_limits.MAX_CONCURRENT_REQUESTS_DESCRIPTION)
    enabled: bool = Field(default=True, description="Whether the server is enabled")
    mcp_apps_enabled: bool = Field(
        default=True,
        description=(
            "Governance flag for the MCP Apps extension. When False, the gateway "
            "suppresses `_meta.ui.resourceUri` for this server (the portal will not "
            "render iframe-based UI bundles for any of its tools). Default True."
        ),
    )
    wants_listener: bool = Field(
        default=True,
        description=(
            "Streamable-HTTP SSE listener affinity. Operator override for the "
            "gateway-derived value (auto-set during capability discovery from the "
            "upstream's `initialize` response). When False, the gateway short-"
            "circuits listener GETs to 405 and the portal SDK skips opening the "
            "listener stream — preventing the SSE reconnect loop that pure "
            "request/response servers otherwise produce. Default True; the "
            "first discovery pass overwrites this with the truth."
        ),
    )
    tenant_limits: Optional[McpLimits] = Field(
        default=None,
        description="Tenant-wide MCP limits of this server, an MCP limits document: every group's limits on the server are folded with them, the most restrictive applying. execution.memory_limit_mb, execution.cpu_limit_percent and data.max_context_window_percent, max_rows_per_query and max_items_per_response are not available in this release and are refused.",
    )
    tenant_governance: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Tenant-wide default MCP governance/policy; min-wins with RG.",
    )
    region: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Geographic region this MCP server serves from (us-east-1, "
            "eu-west-1, …), or 'global' for no specific commitment. Read by "
            "the residency check in the Rust proxy and intersected with the "
            "tenant's data_residency profile; NULL = unspecified, and the "
            "tenant's block_global flag decides whether unspecified is "
            "allowed. Lowercase alphanumeric plus hyphen/underscore."
        ),
    )

    @field_validator("region")
    @classmethod
    def _check_region(cls, value: Optional[str]) -> Optional[str]:
        # Same token rule as tenants.data_residency.allowed_regions and
        # llm_providers.region — the runtime literal-compares the three,
        # so a stray space or uppercase letter here would silently fail
        # every residency match.
        return validate_region(value)

    @field_validator("tenant_limits")
    @classmethod
    def _tenant_limits_readable(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _readable_tenant_limits(value)

    @field_validator("tenant_governance")
    @classmethod
    def _tenant_governance_known_access(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _known_default_access(value)

    @field_validator("max_concurrent_requests", mode="before")
    @classmethod
    def _max_concurrent_requests_positive(cls, value: Any) -> Any:
        problem = mcp_exec_limits.max_concurrent_requests_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class ServerUpdateRequest(ResourceVersionField):
    """Request model for updating an existing MCP server"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Server display name")
    description: Optional[str] = Field(None, description="Server description")
    base_url: Optional[str] = Field(None, description="Server base URL")
    mcp_endpoint_path: Optional[str] = Field(None, description="MCP endpoint path")
    auth_type: Optional[str] = Field(
        None,
        description="Authentication type (none, bearer, oauth)"
    )
    auth_token: Optional[str] = Field(
        default=None,
        description=(
            "The static token the gateway sends to the server as a bearer. Write-only. Leave it out to "
            "keep the stored token; send an empty string or null to remove it; send a value to replace "
            "it. Stored encrypted, never returned; refused with 503 where this control plane has no "
            "encryption service (ENCRYPTION_KEY). A token belongs to `auth_type` bearer: saving the "
            "server with another auth type removes the stored token (`has_auth_token` is then false), "
            "and a token sent with another auth type is refused."
        ),
    )
    idp_id: Optional[str] = Field(None, description="Identity Provider ID for OAuth")
    timeout: Optional[float] = Field(None, description="Request timeout in seconds")
    max_concurrent_requests: Optional[int] = Field(None, ge=1, description=mcp_exec_limits.MAX_CONCURRENT_REQUESTS_DESCRIPTION)
    enabled: Optional[bool] = Field(None, description="Whether the server is enabled")
    mcp_apps_enabled: Optional[bool] = Field(
        None,
        description=(
            "Governance flag for the MCP Apps extension. Set False to suppress "
            "`_meta.ui.resourceUri` for this server."
        ),
    )
    wants_listener: Optional[bool] = Field(
        None,
        description=(
            "Operator override for the SSE listener affinity flag. Normally "
            "auto-derived during capability discovery; set explicitly here to "
            "force-true (re-open the listener for a server we wrongly classified "
            "as non-listener) or force-false (pre-empt the SSE reconnect loop "
            "for a server that falsely advertises listener capabilities)."
        ),
    )
    tenant_limits: Optional[McpLimits] = Field(
        default=None,
        description="Tenant-wide MCP limits of this server, an MCP limits document: every group's limits on the server are folded with them, the most restrictive applying. execution.memory_limit_mb, execution.cpu_limit_percent and data.max_context_window_percent, max_rows_per_query and max_items_per_response are not available in this release and are refused.",
    )
    tenant_governance: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Tenant-wide default MCP governance/policy; min-wins with RG.",
    )
    region: Optional[str] = Field(
        default=None,
        max_length=32,
        description=(
            "Geographic region this MCP server serves from (us-east-1, "
            "eu-west-1, …), or 'global' for no specific commitment. Read by "
            "the residency check in the Rust proxy and intersected with the "
            "tenant's data_residency profile; NULL = unspecified, and the "
            "tenant's block_global flag decides whether unspecified is "
            "allowed. Lowercase alphanumeric plus hyphen/underscore."
        ),
    )

    @field_validator("region")
    @classmethod
    def _check_region(cls, value: Optional[str]) -> Optional[str]:
        # Same token rule as tenants.data_residency.allowed_regions and
        # llm_providers.region — the runtime literal-compares the three,
        # so a stray space or uppercase letter here would silently fail
        # every residency match.
        return validate_region(value)

    @field_validator("tenant_limits")
    @classmethod
    def _tenant_limits_readable(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _readable_tenant_limits(value)

    @field_validator("tenant_governance")
    @classmethod
    def _tenant_governance_known_access(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return _known_default_access(value)

    @field_validator("max_concurrent_requests", mode="before")
    @classmethod
    def _max_concurrent_requests_positive(cls, value: Any) -> Any:
        problem = mcp_exec_limits.max_concurrent_requests_problem(value)
        if problem:
            raise ValueError(problem)
        return value


# ============================================================================
# Response Models - Nested Components
# ============================================================================

class IconInfo(BaseModel):
    """Icon information for a server"""
    type: Optional[str] = Field(None, description="Icon type (file, url, base64)")
    mime_type: Optional[str] = Field(None, description="MIME type of the icon")
    size: Optional[int] = Field(None, description="Icon size in bytes")
    uploaded_at: Optional[str] = Field(None, description="Upload timestamp (ISO 8601)")
    url: Optional[str] = Field(None, description="URL to access the icon")
    available_sizes: Optional[List[str]] = Field(
        None,
        description="Available icon sizes (small, medium, large)"
    )


class CapabilitiesSummary(BaseModel):
    """Summary of server capabilities"""
    tools_count: int = Field(..., description="Number of tools available")
    resources_count: int = Field(..., description="Number of resources available")
    resource_templates_count: int = Field(
        ...,
        description="Number of resource templates available"
    )
    prompts_count: int = Field(..., description="Number of prompts available")
    has_capabilities: bool = Field(
        ...,
        description="Whether capabilities have been discovered"
    )


class CheckedByInfo(BaseModel):
    """Information about who performed the health check"""
    user_id: str = Field(..., description="ID of the admin who performed the check")
    username: str = Field(..., description="Username of the admin")
    admin_type: str = Field(..., description="Type of admin (system or tenant)")

class ServerHealthStatus(BaseModel):
    """Health status embedded in server responses"""
    status: str
    last_check: Optional[str] = None
    last_success: Optional[str] = None
    consecutive_failures: int = 0
    error_message: Optional[str] = None

class MCPServerHealthStatusResponse(BaseModel):
    """Response model for individual server health check"""
    server_id: str = Field(..., description="Server ID")
    server_name: str = Field(..., description="Server name")
    server_url: str = Field(..., description="Server MCP URL")
    status: str = Field(..., description="Health status: healthy, unhealthy, or error")
    connected: bool = Field(..., description="Whether connection was successful")
    timestamp: str = Field(..., description="ISO 8601 timestamp of the check")
    health_status: str = Field(..., description="Database health status")
    check_duration_ms: float = Field(..., description="Duration of health check in milliseconds")
    error: Optional[str] = Field(None, description="Error message if unhealthy")
    checked_by: Optional[CheckedByInfo] = Field(None, description="Who performed the check")

    class Config:
        json_schema_extra = {
            "example": {
                "server_id": "weather-api",
                "server_name": "Weather API",
                "server_url": "https://api.weather.com/mcp",
                "status": "healthy",
                "connected": True,
                "timestamp": "2025-11-06T10:30:00Z",
                "health_status": "healthy",
                "check_duration_ms": 156.3,
                "checked_by": {
                    "user_id": "admin-123",
                    "username": "john.doe",
                    "admin_type": "tenant"
                }
            }
        }


class MCPServerBulkHealthCheckResponse(BaseModel):
    """Response model for bulk health check"""
    total: int = Field(..., description="Total number of servers checked")
    checked: int = Field(..., description="Number of checks completed")
    healthy: int = Field(..., description="Number of healthy servers")
    unhealthy: int = Field(..., description="Number of unhealthy servers")
    failed: int = Field(..., description="Number of failed checks (errors)")
    results: List[MCPServerHealthStatusResponse] = Field(..., description="Individual check results")

    class Config:
        json_schema_extra = {
            "example": {
                "total": 3,
                "checked": 3,
                "healthy": 2,
                "unhealthy": 1,
                "failed": 0,
                "results": [
                    {
                        "server_id": "weather-api",
                        "server_name": "Weather API",
                        "server_url": "https://api.weather.com/mcp",
                        "status": "healthy",
                        "connected": True,
                        "timestamp": "2025-11-06T10:30:00Z",
                        "health_status": "healthy",
                        "check_duration_ms": 156.3
                    },
                    {
                        "server_id": "todo-api",
                        "server_name": "Todo API",
                        "server_url": "https://api.todo.com/mcp",
                        "status": "unhealthy",
                        "connected": False,
                        "timestamp": "2025-11-06T10:30:01Z",
                        "health_status": "unhealthy",
                        "check_duration_ms": 10000.0,
                        "error": "Connection timeout"
                    }
                ]
            }
        }

# ============================================================================
# Response Models - Main Server Responses
# ============================================================================

class OAuthPairing(BaseModel):
    """Whether the gateway exchanges an authorization code for this server.

    It does so for an identity provider that is paired with the server: the
    server's own `idp_id`, or the identity provider of an active OAuth
    client attached through one of the server's configurations."""
    paired: bool = Field(..., description="Whether at least one identity provider is paired with the server")
    idp_ids: List[str] = Field(
        default_factory=list,
        description="The identity providers the gateway accepts a code exchange for",
    )
    warning: Optional[str] = Field(
        None,
        description="What to tell the administrator when no identity provider is paired; null when one is",
    )


class ServerResponse(ResourceMetaFields):
    """Complete server information response"""
    id: str = Field(..., description="Unique server ID")
    name: str = Field(..., description="Server display name")
    description: Optional[str] = Field(None, description="Server description")
    base_url: str = Field(..., description="Server base URL")
    mcp_endpoint_path: str = Field(..., description="MCP endpoint path")
    url: str = Field(..., description="Full MCP endpoint URL")
    auth_type: str = Field(..., description="Authentication type")
    has_auth_token: bool = Field(
        False, description="Whether a static token is stored for this server. The token itself is never returned."
    )
    idp_id: Optional[str] = Field(None, description="Identity Provider ID for OAuth")
    idp_name: Optional[str] = Field(None, description="Identity Provider display name")
    oauth_pairing: Optional["OAuthPairing"] = Field(
        None,
        description=(
            "For an OAuth-protected server (auth_type oauth): whether an identity provider is paired "
            "with it. Null for any other server."
        ),
    )
    timeout: float = Field(..., description="Request timeout in seconds")
    max_concurrent_requests: Optional[int] = Field(None, description=mcp_exec_limits.MAX_CONCURRENT_REQUESTS_DESCRIPTION)
    enabled: bool = Field(..., description="Whether the server is enabled")
    capabilities: Optional[Dict[str, Any]] = Field(
        None,
        description="Discovered server capabilities"
    )
    discovered_at: Optional[str] = Field(
        None,
        description="Capability discovery timestamp (ISO 8601)"
    )
    protocol_version: Optional[str] = Field(
        None,
        description=(
            "Newest MCP protocol version this upstream negotiated at "
            "capability discovery (e.g. 2025-03-26, 2026-07-28). "
            "NULL until discovered. Written by the gateway."
        )
    )
    dialect: Optional[str] = Field(
        None,
        description=(
            "MCP wire dialect the upstream speaks: 'handshake' "
            "(initialize/session flow, spec <= 2025-11-25) or "
            "'stateless' (spec >= 2026-07-28). NULL until discovered; the "
            "gateway treats NULL as 'handshake'."
        )
    )
    wants_listener: bool = Field(
        True,
        description=(
            "Does this server want clients to open the optional "
            "Streamable-HTTP SSE listener channel? Derived from "
            "the `initialize` response capabilities — true iff "
            "the server declares any of tools.listChanged, "
            "resources.listChanged/subscribe, prompts.listChanged, "
            "or logging. When false, the gateway short-circuits "
            "listener GETs to 405 and the portal SDK skips "
            "opening the listener stream, preventing reconnect "
            "loops against pure request/response servers."
        )
    )
    server_info: Optional[Dict[str, Any]] = Field(
        None,
        description="Server metadata (name, version)"
    )
    health_status: Optional[ServerHealthStatus] = Field(
        None,
        description="Current health status"
    )
    capabilities_summary: CapabilitiesSummary = Field(
        ...,
        description="Summary of capabilities"
    )
    icon: Optional[IconInfo] = Field(None, description="Server icon information")
    tenant_limits: Optional[Dict[str, Any]] = Field(
        None, description="Tenant-wide default MCP usage limits (min-wins with RG)."
    )
    tenant_governance: Optional[Dict[str, Any]] = Field(
        None, description="Tenant-wide default MCP governance/policy (min-wins with RG)."
    )
    region: Optional[str] = Field(
        None,
        description=(
            "Geographic region this MCP server serves from; NULL = "
            "unspecified. Read by the Rust residency check."
        ),
    )


class ServerListResponse(BaseModel):
    """Response for listing multiple servers"""
    servers: Dict[str, ServerResponse] = Field(
        ...,
        description="Dictionary of server IDs to server information"
    )
    total: int = Field(..., description="Total number of servers")


class ServerDetailResponse(BaseModel):
    """Response for getting a single server's details"""
    server: ServerResponse = Field(..., description="Server information")


class PoisonedToolFinding(BaseModel):
    """One tool whose metadata matched the prompt-injection
    detector during registration-time scanning. Returned inline on
    Create / Update so the admin UI can show a synchronous warning
    modal at click-time. The async Mission Control system_alert is
    also raised in parallel.

    A single tool can produce MULTIPLE PoisonedToolFinding rows
    when more than one of its metadata fields trips the detector
    (e.g. both a poisoned `name` and a poisoned `description` →
    two findings, two alerts, each acknowledged independently).
    `field_kind` names the offending field."""
    tool_name: str = Field(..., description="Name of the affected tool")
    field_kind: str = Field(
        "description",
        description="Which tool metadata field tripped the detector: 'description', 'name', or 'title'",
    )
    description_excerpt: str = Field(
        ...,
        description="First 500 chars of the matched field's text (named `description_excerpt` for backward compat — carries whichever field tripped per field_kind)",
    )
    description_truncated: bool = Field(
        ...,
        description="True if the matched field's text was longer than 500 chars",
    )
    confidence: float = Field(
        ...,
        description="Detector confidence (0.0-1.0); lower bound = pattern matched, upper bound = high-signal canonical injection",
    )
    patterns_matched: int = Field(
        ...,
        description="Number of injection-pattern matches in the field",
    )
    suspicious_indicator_count: int = Field(
        ...,
        description="Count of suspicious keyword indicators (ignore, override, etc.)",
    )


class ServerCreateResponse(BaseModel):
    """Response after creating a server"""
    message: str = Field(..., description="Success message")
    server: ServerResponse = Field(..., description="Created server information")
    poisoned_tools: List[PoisonedToolFinding] = Field(
        default_factory=list,
        description=(
            "THREAT-01 findings from the registration-time prompt-"
            "injection scan over the upstream's tools/list "
            "descriptions. Empty when no description tripped the "
            "detector. Populated only when both `mcp_registration_enabled` "
            "AND `prompt_injection_enabled` are on for an active "
            "guardrail config in this tenant."
        ),
    )


class RefreshCapabilitiesResponse(BaseModel):
    """Response from the one-click `POST /mcp-servers/{id}/discover-capabilities`
    action. Returns synchronous counts and any inline poison findings so
    the admin UI can render a toast (and the warn-modal when something
    tripped) without a follow-up GET. Capability drift is detected
    inside `discover_capabilities` and raised as a `resource_health`
    system_alert — clients can deep-link into Mission Control from the
    toast if `drift_detected` is true.
    """

    success: bool = Field(..., description="Whether discovery completed")
    server_id: str = Field(..., description="Server that was refreshed")
    discovered: bool = Field(
        ...,
        description=(
            "True when discovery actually ran. False when skipped — "
            "e.g. an OAuth server with no stored access token (the "
            "OAuth login modal handles that path)."
        ),
    )
    tools_count: int = Field(0, description="Tools advertised after refresh")
    resources_count: int = Field(0, description="Resources advertised after refresh")
    resource_templates_count: int = Field(
        0, description="Resource templates advertised after refresh"
    )
    prompts_count: int = Field(0, description="Prompts advertised after refresh")
    drift_detected: bool = Field(
        False,
        description=(
            "True when the diff against the previous snapshot was "
            "non-empty (added/removed/renamed tool, mutated description). "
            "The full diff is in the `resource_health` system_alert."
        ),
    )
    poisoned_tools: List[PoisonedToolFinding] = Field(
        default_factory=list,
        description=(
            "THREAT-01 findings from re-scanning the refreshed tools/list. "
            "Same shape as `ServerCreateResponse.poisoned_tools`."
        ),
    )
    message: Optional[str] = Field(
        None,
        description=(
            "Human-readable summary for the toast, e.g. "
            "'Refreshed: 6 tools' or a skip reason for OAuth servers."
        ),
    )


class ServerUpdateResponse(BaseModel):
    """Response after updating a server"""
    message: str = Field(..., description="Success message")
    server: ServerResponse = Field(..., description="Updated server information")


# ============================================================================
# Connection and Health Check Responses
# ============================================================================

class ExistingIdPInfo(BaseModel):
    """Information about an existing IdP that matches"""
    id: str = Field(..., description="Existing IdP ID")
    name: str = Field(..., description="Existing IdP name")
    enabled: bool = Field(..., description="Whether IdP is enabled")
    issuer: str = Field(..., description="IdP issuer URL")

class OAuthDiscoveryInfo(BaseModel):
    """OAuth discovery information from www-authenticate header"""
    auth_scheme: str = Field(..., description="Authentication scheme (e.g., Bearer)")
    error: Optional[str] = Field(None, description="OAuth error code")
    error_description: Optional[str] = Field(None, description="Human-readable error description")
    resource_metadata: Optional[str] = Field(None, description="Well-known URL for resource metadata")
    realm: Optional[str] = Field(None, description="Authentication realm")
    scope: Optional[str] = Field(None, description="Required OAuth scopes")

    # Discovered IdP information
    issuer: Optional[str] = Field(None, description="Discovered OAuth issuer")
    authorization_endpoint: Optional[str] = Field(None, description="Authorization endpoint")
    token_endpoint: Optional[str] = Field(None, description="Token endpoint")
    well_known_url: Optional[str] = Field(None, description="OIDC discovery URL")

    # Discovery metadata
    discovery_method: Optional[str] = Field(
        None,
        description="How OAuth was discovered (www_authenticate, body_analysis)"
    )
    oauth_docs_url: Optional[str] = Field(
        None,
        description="Provider's OAuth documentation URL"
    )


class SuggestedIdPConfig(BaseModel):
    """Suggested IdP configuration from auto-discovery"""
    id: str = Field(..., description="Generated IdP ID")
    name: str = Field(..., description="Generated IdP name")
    issuer: Optional[str] = Field(None, description="OAuth issuer URL")
    type: str = Field(..., description="IdP type (oidc_discoverable, skeleton, etc.)")
    well_known_url: Optional[str] = Field(None, description="OIDC discovery URL")
    authorization_endpoint: Optional[str] = Field(None, description="Authorization endpoint")
    token_endpoint: Optional[str] = Field(None, description="Token endpoint")
    jwks_uri: Optional[str] = Field(None, description="JWKS endpoint")
    userinfo_endpoint: Optional[str] = Field(None, description="UserInfo endpoint")
    revocation_endpoint: Optional[str] = Field(None, description="Token revocation endpoint")
    introspection_endpoint: Optional[str] = Field(None, description="Token introspection endpoint")
    scopes_supported: Optional[List[str]] = Field(None, description="Supported OAuth scopes")
    response_types_supported: Optional[List[str]] = Field(None, description="Supported response types")
    grant_types_supported: Optional[List[str]] = Field(None, description="Supported grant types")
    token_endpoint_auth_methods: Optional[List[str]] = Field(None, description="Token endpoint auth methods")
    supports_pkce: Optional[bool] = Field(None, description="Whether PKCE is supported")
    description: Optional[str] = Field(None, description="Auto-generated description")
    skeleton: bool = Field(
        default=False,
        description="Whether this is a skeleton config needing manual endpoint setup"
    )
    notes: Optional[str] = Field(
        None,
        description="Notes about the configuration (e.g., manual setup instructions)"
    )


class ConnectionTestResponse(BaseModel):
    """Enhanced response from connection test with OAuth discovery"""
    status: str
    connected: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float
    latency: Optional[float] = None
    suggestions: Optional[List[str]] = None

    # Server information if connected
    server_info: Optional[Dict[str, Any]] = None
    capabilities_summary: Optional[Dict[str, int]] = None

    # HTTP connection details
    http_status: Optional[int] = None
    content_type: Optional[str] = None

    # OAuth discovery information
    oauth_discovery: Optional[OAuthDiscoveryInfo] = None

    # Auto-discovered IdP suggestion
    suggested_idp_config: Optional[SuggestedIdPConfig] = None

    # IdP ID (existing or suggested)
    idp_id: Optional[str] = Field(None, description="IdP ID (existing or suggested)")

    # Existing IdP match
    existing_idp: Optional[ExistingIdPInfo] = None