# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity Provider (IdP) Models
Pydantic models for IdP configuration and management endpoints
"""

from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator, ConfigDict

from app.composition.identity import IDENTITY_PROTOCOLS
from app.constants.oauth import GrantType


# Accepted `aud` values for workload-identity (agent) tokens.
_MAX_WORKLOAD_AUDIENCES = 20
_WORKLOAD_AUDIENCES_DESCRIPTION = (
    "Accepted `aud` values for workload-identity (agent) tokens from this "
    "IdP. A workload token is accepted only when its `aud` matches one of "
    "these. Empty or null: this IdP authenticates no workloads."
)


def _normalise_workload_audiences(v: Optional[List[str]]) -> Optional[List[str]]:
    """Trim, reject empty entries, dedupe (order kept), cap the count."""
    if v is None:
        return None
    out: List[str] = []
    for raw in v:
        if not isinstance(raw, str):
            raise ValueError("workload_audiences entries must be strings")
        value = raw.strip()
        if not value:
            raise ValueError("workload_audiences entries must not be empty")
        if len(value) > 500:
            raise ValueError("workload_audiences entries must be at most 500 characters")
        if value not in out:
            out.append(value)
    if len(out) > _MAX_WORKLOAD_AUDIENCES:
        raise ValueError(
            f"workload_audiences allows at most {_MAX_WORKLOAD_AUDIENCES} entries"
        )
    return out


class IdPConfigRequest(BaseModel):
    """Request model for creating an identity provider.

    The protocol is `oidc` (OIDC and plain OAuth 2.0); the identity
    protocol registry (`app/composition/identity.py`) refuses any other,
    and a field the model does not declare is refused."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Display name for the IdP", min_length=1, max_length=100)
    issuer: str = Field(..., description="IdP issuer URL")
    type: str = Field(..., description="IdP type: oidc_discoverable, manual, github, google, okta, etc.")
    protocol: str = Field(
        "oidc",
        description="Wire protocol: 'oidc' (OIDC and plain OAuth 2.0), the only one in this release",
    )
    # Discovery
    well_known_url: Optional[str] = Field(None, description="OIDC discovery endpoint URL")
    # Manual endpoints (for non-OIDC compliant IdPs)
    authorization_endpoint: Optional[str] = Field(None, description="OAuth authorization endpoint")
    token_endpoint: Optional[str] = Field(None, description="OAuth token endpoint")
    revocation_endpoint: Optional[str] = Field(None, description="Token revocation endpoint")
    introspection_endpoint: Optional[str] = Field(None, description="Token introspection endpoint")
    jwks_uri: Optional[str] = Field(None, description="JWKS endpoint for token verification")
    userinfo_endpoint: Optional[str] = Field(None, description="UserInfo endpoint")
    registration_endpoint: Optional[str] = Field(None, description="RFC 7591 Dynamic Client Registration endpoint")
    # Capabilities
    supports_pkce: bool = Field(True, description="Whether IdP supports PKCE")
    supports_refresh_tokens: bool = Field(True, description="Whether IdP supports refresh tokens")
    supports_client_credentials: bool = Field(False, description="Whether IdP supports client credentials flow")
    token_endpoint_auth_methods: List[str] = Field(
        default=["client_secret_post"],
        description="Supported token endpoint authentication methods"
    )
    # Metadata
    scopes_supported: List[str] = Field(default=[], description="Available OAuth scopes")
    response_types_supported: List[str] = Field(
        default=["code"],
        description="Supported OAuth response types"
    )
    grant_types_supported: List[str] = Field(
        default=[GrantType.AUTHORIZATION_CODE.value, GrantType.REFRESH_TOKEN.value],
        description="Supported OAuth grant types"
    )
    # Cache settings
    discovery_cache_ttl: int = Field(3600, description="Discovery cache TTL in seconds", ge=60)
    # Status
    enabled: bool = Field(..., description="Whether the IdP is enabled")
    verified: bool = Field(False, description="Whether the IdP has been manually verified by admin")
    # Additional info
    description: Optional[str] = Field(None, description="Optional description", max_length=500)
    logo_url: Optional[str] = Field(None, description="Optional logo URL")

    workload_audiences: Optional[List[str]] = Field(
        None, description=_WORKLOAD_AUDIENCES_DESCRIPTION
    )

    @field_validator("workload_audiences")
    @classmethod
    def _validate_workload_audiences(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _normalise_workload_audiences(v)

    @field_validator("protocol")
    @classmethod
    def _validate_protocol(cls, v: str) -> str:
        # A protocol that is not registered is refused, never defaulted.
        IDENTITY_PROTOCOLS.check_protocol(v)
        return v

    @model_validator(mode="after")
    def _validate_against_the_protocols(self) -> "IdPConfigRequest":
        """Reject payloads where protocol and protocol-specific fields
        disagree. The bug class we're guarding against: the legacy form
        let operators pick one protocol AND fill in a field of another —
        the API silently dropped one set. Loud failure beats silent drop.

        What belongs to which protocol, and what a protocol normalises on
        the way in, is the registered protocols' to say
        (`composition/identity.py`); a field of a protocol that is not
        registered is refused."""
        IDENTITY_PROTOCOLS.validate_request(self, _request_defaults())
        return self


class IdPConfigUpdateRequest(BaseModel):
    """Request model for partial IdP updates"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    issuer: Optional[str] = None
    type: Optional[str] = None
    well_known_url: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    introspection_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    registration_endpoint: Optional[str] = None
    supports_pkce: Optional[bool] = None
    supports_refresh_tokens: Optional[bool] = None
    supports_client_credentials: Optional[bool] = None
    token_endpoint_auth_methods: Optional[List[str]] = None
    scopes_supported: Optional[List[str]] = None
    response_types_supported: Optional[List[str]] = None
    grant_types_supported: Optional[List[str]] = None
    discovery_cache_ttl: Optional[int] = Field(None, ge=60)
    enabled: Optional[bool] = None
    verified: Optional[bool] = None
    description: Optional[str] = Field(None, max_length=500)
    logo_url: Optional[str] = None

    protocol: Optional[str] = None
    workload_audiences: Optional[List[str]] = Field(
        None, description=_WORKLOAD_AUDIENCES_DESCRIPTION
    )

    @field_validator("workload_audiences")
    @classmethod
    def _validate_workload_audiences(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _normalise_workload_audiences(v)

    @field_validator("protocol")
    @classmethod
    def _validate_protocol(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        IDENTITY_PROTOCOLS.check_protocol(v)
        return v

    @model_validator(mode="after")
    def _validate_against_the_protocols(self) -> "IdPConfigUpdateRequest":
        """As on create, for what was sent: a field of a protocol that is
        not registered is refused, and the registered protocols normalise
        what they own."""
        IDENTITY_PROTOCOLS.validate_request(self, _request_defaults(), partial=True)
        return self


def _request_defaults() -> Dict[str, Any]:
    """What each field of a create request holds when the caller left it out."""
    return {
        name: field.default
        for name, field in IdPConfigRequest.model_fields.items()
        if not field.is_required()
    }


class IdPDiscoveryResponse(BaseModel):
    """Response model for IdP discovery results"""
    idp_id: str
    idp_name: str
    discovery_successful: bool
    # Discovered endpoints
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    introspection_endpoint: Optional[str] = None
    registration_endpoint: Optional[str] = None
    # Discovered capabilities
    scopes_supported: List[str] = []
    response_types_supported: List[str] = []
    grant_types_supported: List[str] = []
    code_challenge_methods_supported: List[str] = []
    token_endpoint_auth_methods_supported: List[str] = []
    # Analysis
    supports_pkce: bool
    supports_refresh_tokens: bool
    supports_client_credentials: bool
    # Cache info
    discovered_at: str
    cache_ttl_seconds: int
    # Error if discovery failed
    error: Optional[str] = None


class IdPTestResponse(BaseModel):
    """Response model for IdP connectivity test"""
    idp_id: Optional[str] = None
    idp_name: Optional[str] = None
    connected: bool
    endpoints_available: Dict[str, bool] = {}
    discovery_works: bool = False
    response_time_ms: Optional[int] = None
    error: Optional[str] = None
    tested_at: str
    tested_by: Optional[Dict[str, str]] = None


class IdPMatchResult(BaseModel):
    """Result of matching IdP with MCP server requirements"""
    idp_id: str
    idp_name: str
    issuer: str
    suitability_score: int
    reasons: List[str]
    requires_client_registration: bool = True
    supports_pkce: bool
    supports_refresh: bool
    supports_client_credentials: bool
    # Matching details
    scope_coverage: Optional[float] = None
    missing_scopes: List[str] = []


class IdPListResponse(BaseModel):
    """Response model for listing IdPs"""
    idps: Dict[str, Dict[str, Any]]
    total: int
    enabled_count: int
    accessed_by: Optional[Dict[str, str]] = None


class IdPDetailResponse(BaseModel):
    """Response model for IdP details"""
    idp: Dict[str, Any]
    # Runtime status
    is_reachable: Optional[bool] = None
    last_test_at: Optional[str] = None
    accessed_by: Optional[Dict[str, str]] = None


class OAuthSetupGuideResponse(BaseModel):
    """Response model for OAuth setup guide"""
    mcp_server_id: str
    mcp_server_name: str
    oauth_required: bool
    oauth_metadata: Optional[Dict[str, Any]] = None
    compatible_idps: List[IdPMatchResult] = []
    recommended_idp: Optional[str] = None
    setup_steps: List[Dict[str, Any]] = []
    message: Optional[str] = None

class IdPUser(BaseModel):
    """A row that depends on an Identity Provider."""

    id: str
    name: str


class IdPInUse(BaseModel):
    """Why an Identity Provider is not deleted, and what uses it."""

    message: str = Field(..., description="The refusal as a sentence, naming what uses the provider.")
    used_by: Dict[Literal["oauth_app_clients", "mcp_servers", "agent_identities"], List[IdPUser]] = Field(
        ...,
        description=(
            "The tenant's rows that depend on the provider, by kind; a kind nothing of which "
            "uses it is left out."
        ),
    )


class IdPInUseRefusal(BaseModel):
    """The body of the 409 that refuses to delete an Identity Provider in use."""

    detail: IdPInUse
