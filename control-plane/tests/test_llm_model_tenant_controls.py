# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tenant-wide governance/limits columns on ``llm_model``.
These hold a model's TENANT-WIDE default limit/policy posture (same
shapes the Resource Group forms use); the Rust proxy folds them into the
effective (RG, model) limits/param-caps with min-wins.

Pins the schema ⇄ ORM ⇄ Pydantic ⇄ to_dict surface so a save doesn't silently
drop the blob (the "Pydantic strips undeclared fields" gotcha).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from sqlalchemy import JSON

from app.api.v1.models.admin.llm_model import (
    LLModelCreate,
    LLModelResponse,
    LLModelUpdate,
)
from app.database.models.llm_model import LLModel

LIMITS = {"budget": {"monthly_limit_usd": 500.0}, "throughput": {"rpm": 60}}
GOV = {"parameters": {"max_output_tokens": 2048, "temperature_max": 0.7}}


def test_tenant_control_columns_present_and_json():
    cols = {c.name: c for c in LLModel.__table__.columns}
    for name in ("tenant_limits", "tenant_governance"):
        assert name in cols, f"LLModel ORM missing {name}"
        assert isinstance(cols[name].type, JSON)
        assert cols[name].nullable is True


def test_create_and_update_accept_tenant_controls():
    payload = LLModelCreate(
        name="gpt-5.2", provider_id="prov-x", model_name="gpt-5.2", mode="chat",
        tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = payload.model_dump()
    assert d["tenant_limits"] == LIMITS
    assert d["tenant_governance"] == GOV
    # patch-style: only set fields appear
    upd = LLModelUpdate(tenant_limits=LIMITS).model_dump(exclude_unset=True)
    assert upd == {"tenant_limits": LIMITS}


def test_response_declares_tenant_controls():
    fields = LLModelResponse.model_fields
    assert "tenant_limits" in fields
    assert "tenant_governance" in fields


def test_to_dict_surfaces_tenant_controls():
    m = LLModel(
        id="llm-t", tenant_id="t1", name="M", provider_id="p", model_name="m",
        mode="chat", tenant_limits=LIMITS, tenant_governance=GOV,
    )
    d = m.to_dict()
    assert d["tenant_limits"] == LIMITS
    assert d["tenant_governance"] == GOV
