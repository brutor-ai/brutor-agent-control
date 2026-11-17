# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Catalog API Models
Pydantic models for MCP catalog management and import endpoints
"""

from typing import Optional, Dict, Any, List

from pydantic import BaseModel, Field, ConfigDict


# ============================================================================
# Request Models
# ============================================================================

class CatalogEntryCreateRequest(BaseModel):
    """Request model for creating a custom catalog entry"""
    name: str = Field(..., description="Display name")
    slug: str = Field(..., description="URL-safe identifier")
    short_description: Optional[str] = Field(None, description="One-line summary")
    description: Optional[str] = Field(None, description="Full markdown description")
    icon_url: Optional[str] = Field(None, description="External icon URL")
    repo_url: Optional[str] = Field(None, description="Source repository URL")
    runtime: str = Field(..., description="'remote' or 'containerized'")
    remote_config: Optional[Dict[str, Any]] = Field(None, description="Remote config: {fixedURL?, hostname?, headers?}")
    containerized_config: Optional[Dict[str, Any]] = Field(None, description="Docker config: {image, port, path, args?}")
    env_vars: Optional[List[Dict[str, Any]]] = Field(default=[], description="Environment variables")
    tool_preview: Optional[List[Dict[str, Any]]] = Field(default=[], description="Tool previews")
    categories: Optional[List[str]] = Field(default=[], description="Category tags")
    metadata_extra: Optional[Dict[str, Any]] = Field(None, description="Extra metadata")
    auth_pattern: str = Field(default="none", description="'oauth', 'api_key', 'header', 'none'")
    enabled: bool = Field(default=True, description="Whether the entry is enabled")
    is_global: bool = Field(
        default=False,
        description=(
            "Create a GLOBAL system catalog entry (tenant_id NULL, "
            "source='system') instead of a per-tenant custom entry. "
            "Requires system-admin privileges."
        ),
    )


class CatalogEntryUpdateRequest(BaseModel):
    """Request model for updating a catalog entry"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, description="Display name")
    slug: Optional[str] = Field(None, description="URL-safe identifier")
    short_description: Optional[str] = Field(None, description="One-line summary")
    description: Optional[str] = Field(None, description="Full markdown description")
    icon_url: Optional[str] = Field(None, description="External icon URL")
    repo_url: Optional[str] = Field(None, description="Source repository URL")
    runtime: Optional[str] = Field(None, description="'remote' or 'containerized'")
    remote_config: Optional[Dict[str, Any]] = Field(None, description="Remote config")
    containerized_config: Optional[Dict[str, Any]] = Field(None, description="Docker config")
    env_vars: Optional[List[Dict[str, Any]]] = Field(None, description="Environment variables")
    tool_preview: Optional[List[Dict[str, Any]]] = Field(None, description="Tool previews")
    categories: Optional[List[str]] = Field(None, description="Category tags")
    metadata_extra: Optional[Dict[str, Any]] = Field(None, description="Extra metadata")
    auth_pattern: Optional[str] = Field(None, description="Auth pattern")
    enabled: Optional[bool] = Field(None, description="Whether the entry is enabled")


class CatalogImportCredentials(BaseModel):
    """Credentials for importing an MCP server from catalog"""
    client_id: Optional[str] = Field(None, description="OAuth client ID")
    client_secret: Optional[str] = Field(None, description="OAuth client secret")
    api_key: Optional[str] = Field(None, description="API key / bearer token")
    header_values: Optional[Dict[str, str]] = Field(None, description="Header name→value mapping")
    env_values: Optional[Dict[str, str]] = Field(None, description="Env var name→value mapping")
    url_vars: Optional[Dict[str, str]] = Field(
        None,
        description="URL template variables. e.g. {'user': 'facebook', 'repo': 'react'} substituted into fixedURL like 'https://gitmcp.io/{user}/{repo}'",
    )


class CatalogImportRequest(BaseModel):
    """Request model for importing a catalog entry as an MCP server"""
    name_override: Optional[str] = Field(None, description="Override the server name")
    credentials: Optional[CatalogImportCredentials] = Field(None, description="Authentication credentials")
    deploy_container: bool = Field(default=False, description="Deploy container immediately (containerized only)")
    enabled: bool = Field(default=False, description="Enable the server after import (default disabled)")


# ============================================================================
# Response Models
# ============================================================================

class CatalogEntryBrief(BaseModel):
    """Brief catalog entry for list views"""
    id: str
    # NULL for GLOBAL system entries (curated by system admins, visible to
    # all tenants); set for per-tenant custom entries.
    tenant_id: Optional[str] = None
    name: str
    slug: str
    short_description: Optional[str] = None
    icon_url: Optional[str] = None
    has_icon: bool = False
    repo_url: Optional[str] = None
    runtime: str
    categories: List[str] = []
    auth_pattern: str = "none"
    source: str = "system"
    enabled: bool = True
    # Trust + signature metadata surfaced on lists
    # so the catalog browser UI can render the "Verified" badge
    # without re-fetching per-entry.
    trust_tier: str = "unsigned"
    signed_by: Optional[str] = None
    signed_at: Optional[str] = None
    signature_algorithm: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CatalogEntryDetailResponse(CatalogEntryBrief):
    """Full catalog entry detail"""
    description: Optional[str] = None
    remote_config: Optional[Dict[str, Any]] = None
    containerized_config: Optional[Dict[str, Any]] = None
    env_vars: List[Dict[str, Any]] = []
    tool_preview: List[Dict[str, Any]] = []
    metadata_extra: Optional[Dict[str, Any]] = None


class CatalogEntryListResponse(BaseModel):
    """Paginated catalog entry list"""
    entries: List[CatalogEntryBrief]
    total: int


class CategoryCount(BaseModel):
    """Category with entry count"""
    name: str
    count: int


class CatalogCategoriesResponse(BaseModel):
    """List of categories with counts"""
    categories: List[CategoryCount]


class CatalogImportResponse(BaseModel):
    """Result of importing a catalog entry"""
    mcp_server_id: str
    idp_server_id: Optional[str] = None
    oauth_client_id: Optional[str] = None
    server_config_id: str
    docker_deployment_id: Optional[str] = None
    message: str
    # Operator-facing warnings raised by the post-deploy probe. Empty
    # list = clean import. A typical entry: the wrapped MCP binary
    # crashed on first JSON-RPC and we scraped the container's
    # stderr to surface the reason ("missing CLI flag X"). The admin
    # UI renders these as an amber banner so the operator doesn't
    # leave thinking the import was clean when the server is failing
    # on every request.
    deployment_warnings: list[str] = []
    # Tail of the container's log when probe failed — for operators
    # who want the raw evidence. None when probe succeeded or the
    # deployment isn't a docker container.
    container_log_excerpt: Optional[str] = None
