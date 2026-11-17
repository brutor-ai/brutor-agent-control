# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression + contract tests for the recurring Pydantic field-drop class on
ResourceGroup responses: a field added to `to_dict()` but not declared on
`ResourceGroupResponse` is silently stripped, so the GET/PATCH response shows
the default and the operator's edit appears to vanish on form reload.

Bugs fixed: is_system / updated_by stripped from every RG response;
a2a_global_limits / a2a_global_governance / media_global_* never round-tripped.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.models.admin.resource_group import ResourceGroupResponse
from app.database import Base
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Full create_all — to_dict() touches relationships (end_user_groups);
    # the conftest JSONB->TEXT shim makes this SQLite-safe.
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _mk_rg(db: Session, **overrides) -> ResourceGroup:
    rg = ResourceGroup(
        id=overrides.pop("id", "rg1"),
        tenant_id="t1",
        name=overrides.pop("name", "Engineering"),
        display_name="Engineering",
        hierarchy_level=0,
        is_active=True,
        inherit_resources=True,
        **overrides,
    )
    db.add(rg)
    db.commit()
    return rg


def test_rg_response_round_trips_previously_dropped_fields(db):
    rg = _mk_rg(
        db,
        is_system=True,
        updated_by="admin-1",
        a2a_global_limits={"max_calls_per_day": 10},
        a2a_global_governance={"allowed_cards": ["x"]},
        media_global_limits={"max_images_per_day": 5},
        media_global_governance={"blocked": True},
        llm_global_governance={"max_temperature": 1.0},
        mcp_global_governance={"blocked_tools": ["danger"]},
        skill_global_limits={"max_skills_per_day": 3},
    )
    resp = ResourceGroupResponse(**rg.to_dict(include_limits=True))

    assert resp.is_system is True
    assert resp.updated_by == "admin-1"
    assert resp.a2a_global_limits == {"max_calls_per_day": 10}
    assert resp.a2a_global_governance == {"allowed_cards": ["x"]}
    assert resp.media_global_limits == {"max_images_per_day": 5}
    assert resp.media_global_governance == {"blocked": True}
    assert resp.llm_global_governance == {"max_temperature": 1.0}
    assert resp.mcp_global_governance == {"blocked_tools": ["danger"]}
    assert resp.skill_global_limits == {"max_skills_per_day": 3}


def test_response_model_declares_every_to_dict_field(db):
    """Contract test that kills the field-drop class: every key emitted by
    to_dict(include_limits=True) must be a declared field on
    ResourceGroupResponse, or Pydantic silently strips it from the response."""
    rg = _mk_rg(db, id="rg2", name="Contract")
    emitted = set(rg.to_dict(include_limits=True).keys())
    declared = set(ResourceGroupResponse.model_fields.keys())
    missing = sorted(emitted - declared)
    assert not missing, (
        "ResourceGroupResponse is missing fields that to_dict() emits "
        f"(they'd be silently stripped): {missing}"
    )
