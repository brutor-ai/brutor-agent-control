# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tenant-wide governance/limits columns on ``agent_skills`` (migration ``a115_skill_tenant_ctrl``).

Pins schema ⇄ ORM ⇄ Pydantic ⇄ to_dict. Mirrors the LLM/MCP tenant-controls
tests for the Skill resource.
"""

from __future__ import annotations

import pytest

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from sqlalchemy import JSON

from app.api.v1.models.admin.agent_skill import (
    AgentSkillCreate,
    AgentSkillResponse,
    AgentSkillUpdate,
)
from app.database.models.agent_skill import AgentSkill

LIMITS = {"execution": {"daily_limit": 100}}
GOV = {"execution_mode": "approval_required", "approval_timeout": 300}


def test_tenant_control_columns_present_and_json():
    cols = {c.name: c for c in AgentSkill.__table__.columns}
    for name in ("tenant_limits", "tenant_governance"):
        assert name in cols, f"AgentSkill ORM missing {name}"
        assert isinstance(cols[name].type, JSON)
        assert cols[name].nullable is True


def test_create_update_response_declare_tenant_controls():
    c = AgentSkillCreate(
        name="demo-skill", description="d", version="1.0.0",
        skill_md_content="# body",
        tenant_limits=LIMITS,
    )
    d = c.model_dump()
    assert d["tenant_limits"] == LIMITS and d["tenant_governance"] is None
    # A skill's own policy is not available in this release
    # (`tests/test_unenforced_controls_refused.py`).
    with pytest.raises(ValueError, match="not available in this release"):
        AgentSkillCreate(
            name="demo-skill", description="d", version="1.0.0", skill_md_content="# body", tenant_governance=GOV
        )
    upd = AgentSkillUpdate(tenant_limits=LIMITS).model_dump(exclude_unset=True)
    assert upd == {"tenant_limits": LIMITS}
    assert "tenant_limits" in AgentSkillResponse.model_fields
    assert "tenant_governance" in AgentSkillResponse.model_fields


def test_to_dict_surfaces_tenant_controls():
    s = AgentSkill(
        id="sk-t", tenant_id="t1", name="s", description="d", version="1.0.0",
        skill_md_content="# body", skill_md_hash="h",
        tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = s.to_dict()
    assert d["tenant_limits"] == LIMITS
    assert d["tenant_governance"] == GOV
