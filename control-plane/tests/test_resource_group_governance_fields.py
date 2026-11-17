# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Design Note 002 — an AI System is a Resource Group (group_type='ai_system'), so the
AI-System documentation fields now live on every ResourceGroup:
owner / intended_use / intended_client_labels / intended_client_systems / lifecycle_stage (+ the pre-existing
eu_ai_act_risk_tier). These tests pin:
  * model → to_dict → ResourceGroupResponse round-trip (strip-undeclared guard),
  * model-level validators for lifecycle_stage and eu_ai_act_risk_tier,
  * the Create/Update Pydantic schemas accept the fields and reject bad enums.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.models.admin.resource_group import (
    ResourceGroupCreate,
    ResourceGroupResponse,
    ResourceGroupUpdate,
)
from app.database import Base
from app.database.models.resource_group import ResourceGroup


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
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
        name=overrides.pop("name", "Claims Copilot"),
        hierarchy_level=0,
        is_active=True,
        inherit_resources=True,
        **overrides,
    )
    db.add(rg)
    db.commit()
    return rg


# --- round-trip ------------------------------------------------------------

def test_governance_fields_round_trip(db):
    rg = _mk_rg(
        db,
        group_type="ai_system",
        owner="ops@acme.com",
        intended_use="Assist claims agents with policy Q&A.",
        intended_client_labels=["Goose", "Internal Portal"],
        intended_client_systems=["sys-caller"],
        lifecycle_stage="active",
        eu_ai_act_risk_tier="high",
    )
    resp = ResourceGroupResponse(**rg.to_dict(include_limits=True))
    assert resp.owner == "ops@acme.com"
    assert resp.intended_use == "Assist claims agents with policy Q&A."
    assert resp.intended_client_labels == ["Goose", "Internal Portal"]
    assert resp.intended_client_systems == ["sys-caller"]
    assert resp.lifecycle_stage == "active"
    assert resp.eu_ai_act_risk_tier == "high"


def test_governance_fields_default_none(db):
    rg = _mk_rg(db, id="rg-empty", name="No Governance")
    data = rg.to_dict()
    assert data["owner"] is None
    assert data["intended_use"] is None
    assert data["intended_client_labels"] is None
    assert data["intended_client_systems"] is None
    assert data["lifecycle_stage"] is None
    # every new key must be a declared response field (strip-undeclared guard)
    declared = set(ResourceGroupResponse.model_fields.keys())
    for k in ("owner", "intended_use", "intended_client_labels",
              "intended_client_systems", "lifecycle_stage"):
        assert k in declared
    # No alias: the untyped name is gone from model and response.
    assert "intended_clients" not in data
    assert "intended_clients" not in declared


# --- model validators ------------------------------------------------------

def test_lifecycle_stage_validator_rejects_bad():
    with pytest.raises(ValueError):
        ResourceGroup(
            id="x", tenant_id="t1", name="n", hierarchy_level=0,
            lifecycle_stage="bogus",
        )
    # a valid one is accepted
    rg = ResourceGroup(
        id="ok", tenant_id="t1", name="n", hierarchy_level=0,
        lifecycle_stage="proposed",
    )
    assert rg.lifecycle_stage == "proposed"
    # None is allowed (most groups aren't AI systems)
    rg2 = ResourceGroup(id="ok2", tenant_id="t1", name="n2", hierarchy_level=0)
    assert rg2.lifecycle_stage is None


def test_eu_ai_act_risk_tier_validator_rejects_bad():
    with pytest.raises(ValueError):
        ResourceGroup(
            id="x", tenant_id="t1", name="n", hierarchy_level=0,
            eu_ai_act_risk_tier="extreme",
        )
    rg = ResourceGroup(
        id="ok", tenant_id="t1", name="n", hierarchy_level=0,
        eu_ai_act_risk_tier="unacceptable",
    )
    assert rg.eu_ai_act_risk_tier == "unacceptable"


# --- Pydantic schemas ------------------------------------------------------

def test_create_schema_accepts_and_validates_fields():
    c = ResourceGroupCreate(
        name="Claims Copilot",
        owner="ops@acme.com",
        intended_use="x",
        intended_client_labels=["Goose"],
        intended_client_systems=["sys-a"],
        lifecycle_stage="active",
    )
    assert c.owner == "ops@acme.com"
    assert c.intended_client_labels == ["Goose"]
    assert c.intended_client_systems == ["sys-a"]
    assert c.lifecycle_stage == "active"
    with pytest.raises(ValidationError):
        ResourceGroupCreate(name="Bad", lifecycle_stage="bogus")


def test_update_schema_distinguishes_clear_from_absent():
    # Explicit null must be in model_fields_set (so the endpoint clears it),
    # while an omitted field must NOT be.
    cleared = ResourceGroupUpdate(owner=None, lifecycle_stage=None,
                                  intended_use=None, intended_client_labels=None,
                                  intended_client_systems=None)
    for f in ("owner", "lifecycle_stage", "intended_use", "intended_client_labels",
              "intended_client_systems"):
        assert f in cleared.model_fields_set

    absent = ResourceGroupUpdate(display_name="x")
    for f in ("owner", "lifecycle_stage", "intended_use", "intended_client_labels",
              "intended_client_systems"):
        assert f not in absent.model_fields_set

    with pytest.raises(ValidationError):
        ResourceGroupUpdate(lifecycle_stage="bogus")


# --- Replay-suite binding ---------------------------------------------------
#
# `replay_suite_id` is what makes the promotion gate consult a suite. It was
# emitted by `to_dict()` and declared on the RESPONSE model but not on the
# update model, so a PATCH carrying it returned 200 and silently dropped it —
# and the only other way to bind was `bind_to_gate` on the build endpoint,
# which mints a *new* suite. An operator who followed the build endpoint's own
# advice (look at the coverage report before binding) had no way to bind the
# suite they had just reviewed.


def test_update_schema_accepts_replay_suite_id():
    bound = ResourceGroupUpdate(replay_suite_id="rps-123")
    assert bound.replay_suite_id == "rps-123"
    assert "replay_suite_id" in bound.model_fields_set


def test_update_schema_distinguishes_unbind_from_absent():
    # Explicit null means "unbind", and the endpoint keys on model_fields_set
    # to tell that apart from "leave it alone".
    unbind = ResourceGroupUpdate(replay_suite_id=None)
    assert "replay_suite_id" in unbind.model_fields_set

    absent = ResourceGroupUpdate(display_name="x")
    assert "replay_suite_id" not in absent.model_fields_set


def test_the_update_model_covers_every_writable_governance_field():
    """Anything the response exposes as governance must be settable.

    This is the class of bug that produced both the `replay_suite_id` gap and
    the `llm_global_governance` one: a field readable on the response, backed
    by a real column, and quietly missing from the update model — so writes
    return 200 and do nothing. Listing them here means the next omission
    fails a test rather than an operator's afternoon.
    """
    from app.api.v1.models.admin.resource_group import ResourceGroupUpdate

    must_be_writable = {
        "owner",
        "intended_use",
        "intended_client_labels",
        "intended_client_systems",
        "lifecycle_stage",
        "autonomy_level",
        "replay_suite_id",
    }
    missing = must_be_writable - set(ResourceGroupUpdate.model_fields)
    assert not missing, f"not settable via PATCH: {sorted(missing)}"
