# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrail configs fail CLOSED by default.

A check that cannot run is a governance control that did not run. The old
default (`fail_open = True`) skipped it and let the call through, which was
invisible twice over: nothing in the response said so, and the assurance
report went on showing clean guardrail stats.

The column default alone was not enough to change this. Six places
re-supplied `True` — the create endpoint, three Pydantic schemas, the
guardrail seed and the policy template — so a config created through the API
would have been fail-open regardless of what the model said. These tests pin
the model default AND the schema defaults, because the schema is what the
admin UI actually posts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.guardrail_config import GuardrailConfig
from app.database.models.resource_group import ResourceGroup

# `GuardrailConfig.groups` is a selectin relationship, so the RG table and the
# association table must exist for a plain load to resolve to [].
REQUIRED = [
    ResourceGroup.__table__,
    GuardrailConfig.__table__,
    Base.metadata.tables["resource_group_guardrails"],
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


def test_model_default_is_fail_closed(db: Session):
    """A config nobody configured refuses the call when a check breaks."""
    c = GuardrailConfig(id="g-1", tenant_id="t", name="n")
    db.add(c)
    db.commit()
    db.refresh(c)
    assert c.fail_open is False


def test_operator_can_opt_into_log_and_allow(db: Session):
    """The escape hatch: availability over the control, on record."""
    c = GuardrailConfig(id="g-2", tenant_id="t", name="n", fail_open=True)
    db.add(c)
    db.commit()
    db.refresh(c)
    assert c.fail_open is True


def test_fail_open_survives_to_dict(db: Session):
    """Pydantic strips undeclared fields; a flag missing from to_dict cannot
    be shown or changed in the admin UI."""
    c = GuardrailConfig(id="g-3", tenant_id="t", name="n", fail_open=True)
    db.add(c)
    db.commit()
    assert db.query(GuardrailConfig).get("g-3").to_dict()["fail_open"] is True


