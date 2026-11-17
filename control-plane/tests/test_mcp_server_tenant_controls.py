# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tenant-wide governance/limits columns on ``mcp_servers`` (migration ``a114_mcp_server_tenant_ctrl``).

Pins schema ⇄ ORM ⇄ Pydantic ⇄ to_dict so a save doesn't silently drop the
blob. Mirrors test_llm_model_tenant_controls for the MCP resource.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from sqlalchemy import JSON

from app.api.v1.models.admin.mcp_server import (
    ServerCreateRequest,
    ServerResponse,
    ServerUpdateRequest,
)
from app.database.models.mcp_server import MCPServer

LIMITS = {"frequency": {"max_calls_per_minute": 60}, "per_tool": {"shell.exec": {"max_calls_per_minute": 5}}}
GOV = {"default_access": "approval_required"}


def test_tenant_control_columns_present_and_json():
    cols = {c.name: c for c in MCPServer.__table__.columns}
    for name in ("tenant_limits", "tenant_governance"):
        assert name in cols, f"MCPServer ORM missing {name}"
        assert isinstance(cols[name].type, JSON)
        assert cols[name].nullable is True


def test_create_update_response_declare_tenant_controls():
    payload = ServerCreateRequest(
        name="s", base_url="https://x", tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = payload.model_dump()
    assert d["tenant_limits"] == LIMITS and d["tenant_governance"] == GOV
    upd = ServerUpdateRequest(tenant_limits=LIMITS).model_dump(exclude_unset=True)
    assert upd == {"tenant_limits": LIMITS}
    assert "tenant_limits" in ServerResponse.model_fields
    assert "tenant_governance" in ServerResponse.model_fields


def test_to_dict_surfaces_tenant_controls():
    s = MCPServer(
        id="mcp-t", tenant_id="t1", name="S", base_url="https://x",
        tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = s.to_dict()
    assert d["tenant_limits"] == LIMITS
    assert d["tenant_governance"] == GOV
