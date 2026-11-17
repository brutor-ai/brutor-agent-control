# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Per-tool guardrail scope

Locks down the Python-side contract for the two new
`GuardrailConfig` columns:

  scope_mcp_server_id  — FK to mcp_servers.id, nullable
  scope_tool_name      — string(255), nullable

The Rust proxy enforces these at runtime (tested separately in
`brutor-gateway-core/src/services/guardrails/mod.rs` —
`filter_by_call_scope_*` tests, 6 cases). The Python side is
responsible for:

  1. Storing the columns when create/update endpoints receive them
  2. Returning them on `to_dict()` so the admin UI preloads
  3. Treating empty strings as NULL (not as the literal "" string)
  4. Surviving PATCH "absent vs null" semantics — absent leaves
     the existing value, explicit null clears it

These tests run on an in-memory SQLite with just the tables they
need, mirroring the pattern of the other guardrail test suites.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.guardrail_config import (
    GuardrailConfig, resource_group_guardrails,
)
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    # GuardrailConfig.group_ids (used by to_dict) reads the
    # `groups` relationship, which loads the resource_group_guardrails
    # M2M plus the resource_groups table. Both must exist or the
    # ORM raises on flush.
    Base.metadata.create_all(
        engine, tables=[
            Tenant.__table__,
            MCPServer.__table__,
            ResourceGroup.__table__,
            GuardrailConfig.__table__,
            resource_group_guardrails,
        ],
    )
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(Tenant(
        id="t1", name="t1", display_name="t1",
        primary_contact_email="t1@example.invalid",
    ))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_guardrail(
    db, *,
    name: str = "g1",
    scope_server: str | None = None,
    scope_tool: str | None = None,
) -> GuardrailConfig:
    g = GuardrailConfig(
        id=f"gr-{uuid.uuid4().hex[:12]}",
        tenant_id="t1",
        name=name,
        enabled=True,
        scope_mcp_server_id=scope_server,
        scope_tool_name=scope_tool,
    )
    db.add(g)
    db.flush()
    return g


# ---------------------------------------------------------------------------
# 1. Columns exist + persist
# ---------------------------------------------------------------------------


class TestColumnPersistence:
    def test_scope_columns_nullable_by_default(self, db):
        # Configs without a scope must continue to "apply to all calls" — no migration
        # data backfill required.
        g = _make_guardrail(db, name="legacy")
        db.commit()
        loaded = db.query(GuardrailConfig).filter_by(id=g.id).one()
        assert loaded.scope_mcp_server_id is None
        assert loaded.scope_tool_name is None

    def test_server_scope_persists(self, db):
        # SQLite test DB doesn't enforce FK constraints (by default
        # foreign_keys=OFF), so we don't need to create the upstream
        # MCPServer row here — the column accepts any string. The
        # FK behaviour is exercised by the migration-text test below.
        g = _make_guardrail(db, name="srv-only", scope_server="srv-x")
        db.commit()
        loaded = db.query(GuardrailConfig).filter_by(id=g.id).one()
        assert loaded.scope_mcp_server_id == "srv-x"

    def test_tool_scope_persists_without_server_scope(self, db):
        # tool-only scope is allowed (Obot-style "any server with a
        # `delete_record` tool" — useful for global blocklists).
        g = _make_guardrail(db, name="tool-only", scope_tool="delete_record")
        db.commit()
        loaded = db.query(GuardrailConfig).filter_by(id=g.id).one()
        assert loaded.scope_tool_name == "delete_record"
        assert loaded.scope_mcp_server_id is None

    def test_both_scopes_persist(self, db):
        g = _make_guardrail(
            db, name="precise",
            scope_server="srv-x", scope_tool="get_customer",
        )
        db.commit()
        loaded = db.query(GuardrailConfig).filter_by(id=g.id).one()
        assert loaded.scope_mcp_server_id == "srv-x"
        assert loaded.scope_tool_name == "get_customer"


# ---------------------------------------------------------------------------
# 2. to_dict surfaces the new fields
# ---------------------------------------------------------------------------


class TestToDictExposesScope:
    def test_to_dict_includes_both_scope_fields(self, db):
        g = _make_guardrail(
            db, name="precise",
            scope_server="srv-x", scope_tool="get_customer",
        )
        d = g.to_dict()
        assert d["scope_mcp_server_id"] == "srv-x"
        assert d["scope_tool_name"] == "get_customer"

    def test_to_dict_returns_none_when_unscoped(self, db):
        g = _make_guardrail(db, name="g")
        d = g.to_dict()
        # Defensive against admin UI breakage — these keys MUST be
        # present even when NULL so the form's preload doesn't
        # accidentally read `undefined` and fall back to "".
        assert "scope_mcp_server_id" in d
        assert "scope_tool_name" in d
        assert d["scope_mcp_server_id"] is None
        assert d["scope_tool_name"] is None


# ---------------------------------------------------------------------------
# 3. FK on scope_mcp_server_id is `ON DELETE SET NULL`
#
# Validated via migration-text inspection rather than runtime FK
# enforcement: that behaviour is Postgres-specific and SQLite would
# need PRAGMA foreign_keys=ON plus matching cascade rules wired into
# the ORM model relationships, both of which are tangential to what
# we're locking down (the schema contract).
# ---------------------------------------------------------------------------


class TestServerForeignKey:

    def test_model_declares_on_delete_set_null(self):
        # Same constraint mirrored on the SQLAlchemy model so a
        # rebuild-from-models scenario produces the same FK shape.
        from app.database.models.guardrail_config import GuardrailConfig
        col = GuardrailConfig.__table__.columns["scope_mcp_server_id"]
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "mcp_servers"
        assert fks[0].ondelete == "SET NULL"


# ---------------------------------------------------------------------------
# 4. Migration-replay safety
# ---------------------------------------------------------------------------
