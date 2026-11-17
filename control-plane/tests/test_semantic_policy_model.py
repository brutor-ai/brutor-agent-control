# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SemanticPolicy model — vocab validation, to_dict surface, and RG binding.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.resource_group import ResourceGroup
from app.database.models.semantic_policy import (
    SemanticPolicy,
    resource_group_semantic_policies,
)

# SQLite (test) does not enforce FKs. The RG-binding behavior is covered by the
# Rust `get_policies` db-test against the real schema; here we pin vocab
# validation + the to_dict surface (the strip-field gotcha guard). The empty
# resource_groups + association tables let the `groups` selectin load resolve to
# [] without cascading into RG's own relationships.
REQUIRED = [
    ResourceGroup.__table__,
    SemanticPolicy.__table__,
    resource_group_semantic_policies,
]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_validates_action_mode_surfaces():
    with pytest.raises(ValueError):
        SemanticPolicy(id="p", tenant_id="t", name="n", constraint_text="c", action="nope")
    with pytest.raises(ValueError):
        SemanticPolicy(id="p", tenant_id="t", name="n", constraint_text="c", mode="nope")
    with pytest.raises(ValueError):
        SemanticPolicy(id="p", tenant_id="t", name="n", constraint_text="c",
                       surfaces=["chat_input", "not_a_surface"])


def test_to_dict_exposes_all_fields(db):
    p = SemanticPolicy(
        id="sgp-1",
        tenant_id="t1",
        name="no-pii-egress",
        constraint_text="agents may not send PII to external peers",
        action="block",
        mode="shadow",
        surfaces=["a2a_outbound"],
    )
    db.add(p)
    db.commit()

    d = db.query(SemanticPolicy).get("sgp-1").to_dict()
    assert d["name"] == "no-pii-egress"
    assert d["action"] == "block"
    assert d["mode"] == "shadow"
    assert d["surfaces"] == ["a2a_outbound"]
    assert d["resource_group_ids"] == []
    # Every documented key is present (strip-field gotcha guard).
    for key in (
        "id", "tenant_id", "name", "description", "constraint_text", "action",
        "mode", "surfaces", "judge_model_id", "enabled", "resource_group_ids",
        "created_by", "created_at", "updated_at",
    ):
        assert key in d, f"missing {key}"


# ---------------------------------------------------------------------------
# Fail-closed by default
#
# The judge-unavailable branch itself lives in the Rust evaluator (pinned by
# `verdict_for_unavailable_judge` unit tests). What must hold here is the
# thing the evaluator reads: a policy created without an explicit choice is
# fail-CLOSED, and the flag survives the to_dict surface so the admin API and
# UI can actually expose the opt-out.
# ---------------------------------------------------------------------------

def test_fail_open_defaults_to_closed(db: Session):
    """A policy nobody configured must refuse on an unavailable judge.

    If this ever defaults true again, every policy silently stops enforcing
    the moment its judge hiccups — the exact inversion
    ``a155_fail_closed_by_default`` closed.
    """
    p = SemanticPolicy(
        id="sgp-fc", tenant_id="t", name="n", constraint_text="no secrets"
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    assert p.fail_open is False


def test_fail_open_is_operator_selectable(db: Session):
    """The opt-in: log-and-allow, chosen deliberately and on record."""
    p = SemanticPolicy(
        id="sgp-fo", tenant_id="t", name="n", constraint_text="c", fail_open=True
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    assert p.fail_open is True


def test_fail_open_survives_to_dict(db: Session):
    """Pydantic strips undeclared fields — a flag absent from to_dict is a
    flag the admin UI can never show or change."""
    p = SemanticPolicy(
        id="sgp-td", tenant_id="t", name="n", constraint_text="c", fail_open=True
    )
    db.add(p)
    db.commit()
    assert db.query(SemanticPolicy).get("sgp-td").to_dict()["fail_open"] is True
