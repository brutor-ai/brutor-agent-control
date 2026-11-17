# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Admin endpoint for Claude Desktop config export

`POST /v1/admin/end-users/{user_id}/claude-desktop-config`

Given a tenant admin's request + a target end_user_id + a Brutor
API key the user already owns (passed in the request body so we
don't have to decrypt anything server-side), this endpoint returns
the JSON the user should paste into their
`claude_desktop_config.json`.

# Why POST (not GET)

The API key value is a credential. URL params end up in proxy
access logs in plaintext; request bodies generally don't. Use POST.

# Why the admin passes the API key (vs the endpoint auto-generating one)

Two reasons:
  1. We never auto-issue credentials from a config-export request.
     An admin operator already has tools for issuing keys; the
     export endpoint is JUST about generating the config blob.
  2. The api_keys table stores HASHED + ENCRYPTED key values.
     The server can't recover the plaintext to embed it. The admin holds the plaintext from the create-key
     response and pastes it here.
"""

from __future__ import annotations

import logging
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.services.claude_desktop.config_export import (
    build_claude_desktop_config,
)


log = logging.getLogger("brutor-claude-desktop-export")


class ClaudeDesktopExportRequest(BaseModel):
    """Body for POST /admin/end-users/{user_id}/claude-desktop-config."""
    api_key: str = Field(
        ..., min_length=20, max_length=500,
        description="Plaintext API key (sk_brutor_api_…) — embedded in the config",
    )
    gateway_url: str = Field(
        ..., min_length=1, max_length=500,
        description="Public URL the user's Claude Desktop will hit (no /v1 suffix)",
    )


class ClaudeDesktopExportResponse(BaseModel):
    """Response shape. The `config` field is the JSON the user will
    paste into their `claude_desktop_config.json`. We DON'T marshal
    it to a string — clients can re-serialise with their own
    formatting preferences (Claude Desktop accepts both 2-space and
    4-space indentation)."""
    config: dict
    mcp_server_count: int
    virtual_server_count: int


def create_claude_desktop_export_router(*, logger=None) -> APIRouter:
    router = APIRouter(prefix="/admin/end-users", tags=["claude-desktop-export"])

    @router.post(
        "/{user_id}/claude-desktop-config",
        response_model=ClaudeDesktopExportResponse,
        summary="Generate Claude Desktop config for an end user",
        description=(
            "Build a `claude_desktop_config.json` blob containing "
            "every MCP / virtual-MCP server the named user can reach "
            "via their resource-group memberships. The admin "
            "operator distributes the response out of band."
        ),
    )
    def export_claude_desktop_config(
        user_id: int,
        body: ClaudeDesktopExportRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.END_USERS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.ADMIN_USER_READ)
        ),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        try:
            config = build_claude_desktop_config(
                db=db,
                tenant_id=tenant_id,
                user_id=user_id,
                gateway_url=body.gateway_url,
                api_key=body.api_key,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        mcp_count = sum(
            1 for k in config["mcpServers"].keys()
            if k.startswith("brutor-mcp-")
        )
        vmcp_count = sum(
            1 for k in config["mcpServers"].keys()
            if k.startswith("brutor-vmcp-")
        )

        log.info(
            "Claude Desktop config exported",
            extra={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "admin_id": admin_obj.id,
                "mcp_server_count": mcp_count,
                "virtual_server_count": vmcp_count,
            },
        )
        return ClaudeDesktopExportResponse(
            config=config,
            mcp_server_count=mcp_count,
            virtual_server_count=vmcp_count,
        )

    return router
