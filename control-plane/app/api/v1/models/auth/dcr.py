# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RFC 7591 Dynamic Client Registration — request/response models

Public, unauthenticated `POST /v1/auth/dcr/{tenant_id}/
{idp_id}/register` lets MCP / OAuth clients self-register against
Brutor. The endpoint produces a Brutor-issued client_id +
client_secret that the client then uses in Brutor's normal OAuth
proxy flow — Brutor mediates with the upstream IdP.

# Spec compliance

The request and response shapes mirror RFC 7591 §2 and RFC 7591 §3
respectively. We don't implement OPTIONAL fields that aren't
useful for the MCP-client use case (sector_identifier_uri,
subject_type, etc.) but we DO echo unrecognised fields back on
the response so a strict-mode client doesn't reject our response
for missing keys.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class DCRRegistrationRequest(BaseModel):
    """RFC 7591 §2 — Client Registration Request.

    Field names match the RFC exactly so a compliant client's
    Pydantic-equivalent on the other side round-trips cleanly.
    """
    redirect_uris: List[str] = Field(
        ...,
        description="OAuth redirect URIs the client will use",
        min_length=1,
        max_length=20,  # cap to defeat memory-bomb requests
    )
    client_name: Optional[str] = Field(
        None, max_length=255,
        description="Human-readable client name (becomes oauth_app_clients.name)",
    )
    client_uri: Optional[str] = Field(
        None, max_length=500,
        description="Public-facing URL of the client homepage",
    )
    logo_uri: Optional[str] = Field(
        None, max_length=500,
        description="Public-facing logo URL",
    )
    scope: Optional[str] = Field(
        None, max_length=2000,
        description="Space-separated list of requested OAuth scopes",
    )
    grant_types: Optional[List[str]] = Field(
        None, max_length=10,
        description="OAuth grant types the client will use",
    )
    response_types: Optional[List[str]] = Field(
        None, max_length=10,
        description="OAuth response types (typically 'code')",
    )
    token_endpoint_auth_method: Optional[str] = Field(
        None, max_length=50,
        description="How the client authenticates to the token endpoint",
    )
    contacts: Optional[List[str]] = Field(
        None, max_length=20,
        description="Email addresses for the client's responsible party",
    )
    tos_uri: Optional[str] = Field(None, max_length=500)
    policy_uri: Optional[str] = Field(None, max_length=500)
    software_id: Optional[str] = Field(None, max_length=255)
    software_version: Optional[str] = Field(None, max_length=100)
    application_type: Optional[str] = Field(
        None, max_length=50,
        description="'web' (default), 'native', or one of: webapp, mobile, desktop, cli",
    )

    @field_validator("redirect_uris", mode="after")
    @classmethod
    def _redirect_uris_must_be_urls(cls, v: List[str]) -> List[str]:
        # RFC 7591 §2.1 implicitly inherits the OAuth URI restrictions:
        # no fragment, must be absolute, http(s) preferred. We don't
        # block `http://localhost` (desktop CLI tools need it) but we
        # do reject obvious junk early so the client gets a clear
        # error instead of a downstream OAuth failure.
        for uri in v:
            if "#" in uri:
                raise ValueError(f"redirect_uri must not contain a fragment: {uri}")
            if not (uri.startswith("http://") or uri.startswith("https://")):
                raise ValueError(f"redirect_uri must be http(s): {uri}")
        return v


class DCRRegistrationResponse(BaseModel):
    """RFC 7591 §3 — Client Information Response.

    `client_secret_expires_at` is always 0 ("never expires") today —
    we don't rotate secrets on a schedule. Operators rotate via the
    admin UI (existing flow).
    """
    client_id: str = Field(..., description="Brutor-issued OAuth client ID")
    client_secret: str = Field(..., description="OAuth client secret (only returned ONCE — store it now)")
    client_id_issued_at: int = Field(..., description="Unix timestamp of issuance")
    client_secret_expires_at: int = Field(
        0, description="0 = never expires. Rotation is admin-driven, not time-based."
    )
    redirect_uris: List[str]
    client_name: Optional[str] = None
    client_uri: Optional[str] = None
    logo_uri: Optional[str] = None
    scope: Optional[str] = None
    grant_types: Optional[List[str]] = None
    response_types: Optional[List[str]] = None
    token_endpoint_auth_method: Optional[str] = None
    application_type: Optional[str] = None
    # RFC 7591 §3.2.2 — point clients to a management endpoint.
    # Today this is informational; a future sprint can ship a
    # client-self-service endpoint (GET / DELETE on this URL).
    registration_client_uri: Optional[str] = None


class DCRErrorResponse(BaseModel):
    """RFC 7591 §3.3 — error envelope. Use ONLY for 4xx responses."""
    error: str = Field(
        ...,
        description=(
            "RFC 7591 error code: invalid_redirect_uri, invalid_client_metadata, "
            "or invalid_software_statement"
        ),
    )
    error_description: Optional[str] = Field(
        None, description="Human-readable error detail"
    )
