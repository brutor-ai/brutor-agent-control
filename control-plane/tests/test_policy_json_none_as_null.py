# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A policy or limit document the core enforces is stored as SQL NULL when
there is none — never as JSON ``null``.

SQLAlchemy's plain ``JSON`` type persists Python ``None`` as the JSON value
``null`` (``should_evaluate_none``). The core reads these columns as
``Option<serde_json::Value>`` and decodes ``Some(Null)`` into a policy
struct, which fails — and an unreadable policy is refused, fail closed.
So a "cleared" policy written as JSON ``null`` refused every call:
live, the Nordica seed's Claude Sonnet 5 model answered 503
"this group's governance cannot be read" on every request; 8 skills and 6
MCP servers carried the same value.

The class guard pins the rule for every column whose name says it holds a
policy or limit document (``…governance`` / ``…limits``), so a new one is
covered the day it is added. The migration ``a205_policy_json_null`` repaired the stored rows.
"""

from __future__ import annotations

import re

from sqlalchemy import JSON, Column

import app.database.models  # noqa: F401 — registers every model on Base
from app.database import Base

POLICY_COLUMN = re.compile(r"(^|_)(governance|limits)$")


def _policy_columns() -> list[tuple[str, Column]]:
    return [
        (f"{t.name}.{c.name}", c)
        for t in Base.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, JSON) and POLICY_COLUMN.search(c.name)
    ]


def test_the_class_is_found():
    names = {n for n, _ in _policy_columns()}
    # The column that refused live traffic, and one per owning surface.
    for expected in (
        "llm_model.tenant_governance",
        "agent_skills.tenant_governance",
        "mcp_servers.tenant_governance",
        "agent_cards.tenant_limits",
        "resource_groups.llm_global_governance",
        "resource_group_agent_skills.governance",
    ):
        assert expected in names


def test_every_nullable_policy_column_stores_none_as_sql_null():
    offenders = sorted(
        name
        for name, col in _policy_columns()
        if col.nullable and col.type.should_evaluate_none
    )
    assert offenders == [], (
        "nullable policy/limit JSON columns that store Python None as JSON null "
        "(declare JSON(none_as_null=True) / JSONB(none_as_null=True)): "
        f"{offenders}"
    )


def test_the_detector_sees_the_old_shape():
    # Proven against the shape it forbids.
    assert JSON().should_evaluate_none is True
    assert JSON(none_as_null=True).should_evaluate_none is False
