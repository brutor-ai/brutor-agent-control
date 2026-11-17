# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
seed_mcp_catalog — global (tenant_id NULL) idempotent seeding (migration `a093_mcp_catalog_global`).

The MCP catalog is GLOBAL now: system entries are seeded ONCE platform-wide
keyed on `mcp-cat-system-{slug}`, never copied per tenant. This pins:
  * Seeded system rows are global (tenant_id IS NULL, source='system').
  * Stable id `mcp-cat-system-{slug}`.
  * Idempotent — a second call inserts nothing new (refresh in place).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.seed_mcp_catalog import seed_mcp_catalog


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[MCPCatalogEntry.__table__])
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionFactory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_seeds_global_system_entries(db):
    seed_mcp_catalog(db, logger=None)
    db.commit()

    rows = db.query(MCPCatalogEntry).all()
    assert rows, "expected the global catalog to be seeded"
    for r in rows:
        assert r.tenant_id is None, f"{r.id} must be global (tenant_id NULL)"
        assert r.source == "system"
        assert r.id == f"mcp-cat-system-{r.slug}"


def test_seed_is_idempotent(db):
    seed_mcp_catalog(db, logger=None)
    db.commit()
    count_first = db.query(MCPCatalogEntry).count()

    # Second run must not add new rows (refresh in place).
    seed_mcp_catalog(db, logger=None)
    db.commit()
    count_second = db.query(MCPCatalogEntry).count()

    assert count_first == count_second
    assert count_first > 0


def test_seed_accepts_legacy_tenant_id_kwarg(db):
    # Back-compat: legacy callers may still pass tenant_id; it is ignored
    # and the catalog is still seeded globally.
    seed_mcp_catalog(db, logger=None, tenant_id="some-tenant")
    db.commit()
    rows = db.query(MCPCatalogEntry).all()
    assert rows
    assert all(r.tenant_id is None for r in rows)
