# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tenant-wide governance/limits columns on ``agent_cards`` (a116_agent_card_tenant_ctrl).

Pins schema ⇄ ORM ⇄ Pydantic ⇄ to_dict. Mirrors the LLM/MCP/Skill
tenant-controls tests for the Agent (A2A) resource.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from sqlalchemy import JSON

from app.api.v1.models.admin.agent_card import (
    AgentCardListItem,
    AgentCardUpdateRequest,
)
from app.database.models.agent_card import AgentCard

LIMITS = {"frequency": {"rpm": 30}, "max_delegation_depth": 3}
GOV = {"require_signed": True}


def test_tenant_control_columns_present_and_json():
    cols = {c.name: c for c in AgentCard.__table__.columns}
    for name in ("tenant_limits", "tenant_governance"):
        assert name in cols, f"AgentCard ORM missing {name}"
        assert isinstance(cols[name].type, JSON)
        assert cols[name].nullable is True


def test_update_and_response_declare_tenant_controls():
    upd = AgentCardUpdateRequest(tenant_limits=LIMITS).model_dump(exclude_unset=True)
    assert upd == {"tenant_limits": LIMITS}
    assert "tenant_limits" in AgentCardListItem.model_fields
    assert "tenant_governance" in AgentCardListItem.model_fields


def test_to_dict_surfaces_tenant_controls():
    c = AgentCard(
        id="ac-t", tenant_id="t1", name="agent", version="1.0.0",
        card_json={"name": "agent"}, trust_tier="unsigned",
        tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = c.to_dict()
    assert d["tenant_limits"] == LIMITS
    assert d["tenant_governance"] == GOV
