# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# VMCP-aware Server Access Configurations

Locks down the XOR backend contract for `ServerConfig`:

    Exactly one of `mcp_server_id` or `virtual_server_id` must
    be set on every server_config row.

Two enforcement points are exercised independently:

  1. **Pydantic** (`ServerConfigCreate`) — request-layer validator.
     Catches admin-UI / direct API misuse before the manager runs.
  2. **Manager** (`ServerConfigManager.create_config`) — defensive
     re-check inside the service so seed paths, tests, and any
     other callers that bypass Pydantic can't write malformed
     rows. The manager is also where the VMCP-specific business
     rules live (VMCP must exist, oauth_client_id rejected,
     uniqueness scoped to virtual_server_id + name).

Tests use an in-memory SQLite so they run fast and isolated. The
Postgres CHECK constraint in migration a049_server_config_vmcp_support is the ultimate
backstop — that's a deployment-time guarantee, not unit-tested
here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant
from app.database.models.virtual_server import VirtualServer
from app.api.v1.models.admin.server_config import ServerConfigCreate
from app.services.portal.server_config_manager import ServerConfigManager


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            MCPServer.__table__,
            VirtualServer.__table__,
            OAuthAppClient.__table__,
            ResourceGroup.__table__,
            ServerConfig.__table__,
            resource_group_server_configs,
        ],
    )
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(
        Tenant(
            id="t1", name="t1", display_name="t1",
            primary_contact_email="t1@example.invalid",
        )
    )
    session.add(
        Tenant(
            id="t2", name="t2", display_name="t2",
            primary_contact_email="t2@example.invalid",
        )
    )
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_mcp(db, *, server_id="mcp-1", tenant_id="t1", name="Test MCP", auth_type="none"):
    s = MCPServer(
        id=server_id, tenant_id=tenant_id, name=name,
        base_url=f"https://{server_id}.example.invalid",
        auth_type=auth_type, enabled=True,
    )
    db.add(s)
    db.flush()
    return s


def _seed_vmcp(db, *, vs_id="vs-1", tenant_id="t1", name="Test VMCP", enabled=True):
    v = VirtualServer(
        id=vs_id, tenant_id=tenant_id, name=name,
        owner="1", enabled=enabled,
        capabilities={"tools": [], "resources": [], "prompts": []},
    )
    db.add(v)
    db.flush()
    return v


# ---------------------------------------------------------------------------
# 1. Pydantic XOR validator — request layer
# ---------------------------------------------------------------------------


class TestPydanticXOR:
    def test_mcp_only_is_valid(self):
        """Pre-Sprint-4.1 contract still works: mcp_server_id alone."""
        m = ServerConfigCreate(
            mcp_server_id="mcp-1",
            name="My Config",
        )
        assert m.mcp_server_id == "mcp-1"
        assert m.virtual_server_id is None

    def test_vmcp_only_is_valid(self):
        """New shape: virtual_server_id alone."""
        m = ServerConfigCreate(
            virtual_server_id="vs-1",
            name="My VMCP Config",
        )
        assert m.virtual_server_id == "vs-1"
        assert m.mcp_server_id is None

    def test_both_set_rejected(self):
        """The whole point of the XOR — both must not be accepted."""
        with pytest.raises(ValueError, match=r"(?i)not both|exactly one"):
            ServerConfigCreate(
                mcp_server_id="mcp-1",
                virtual_server_id="vs-1",
                name="Conflicted",
            )

    def test_neither_set_rejected(self):
        """Empty config is malformed — at least one backend required."""
        with pytest.raises(ValueError, match=r"(?i)must set one"):
            ServerConfigCreate(name="Floating Config")


# ---------------------------------------------------------------------------
# 2. Manager XOR re-check — defense in depth
# ---------------------------------------------------------------------------


class TestManagerXORReCheck:
    """The manager accepts dicts in addition to Pydantic models,
    which means seed paths and tests can bypass Pydantic. So the
    manager re-runs the XOR check itself."""

    async def test_both_backends_rejected(self, db):
        mgr = ServerConfigManager()
        with pytest.raises(ValueError, match=r"(?i)exactly one backend"):
            await mgr.create_config(
                config={
                    "mcp_server_id": "mcp-1",
                    "virtual_server_id": "vs-1",
                    "name": "Bad",
                },
                db=db,
                tenant_id="t1",
            )

    async def test_neither_backend_rejected(self, db):
        mgr = ServerConfigManager()
        with pytest.raises(ValueError, match=r"(?i)exactly one backend"):
            await mgr.create_config(
                config={"name": "Empty"},
                db=db,
                tenant_id="t1",
            )


# ---------------------------------------------------------------------------
# 3. Manager VMCP branch — happy path + rules specific to the VMCP backend
# ---------------------------------------------------------------------------


class TestManagerVMCPBranch:
    async def test_unknown_vmcp_rejected(self, db):
        mgr = ServerConfigManager()
        with pytest.raises(ValueError, match=r"not found in tenant"):
            await mgr.create_config(
                config=ServerConfigCreate(
                    virtual_server_id="vs-does-not-exist",
                    name="Phantom",
                ),
                db=db,
                tenant_id="t1",
            )

    async def test_cross_tenant_vmcp_rejected(self, db):
        # VMCP exists, but in a different tenant. Manager must
        # treat that as "not found" to preserve tenant isolation.
        _seed_vmcp(db, vs_id="vs-foreign", tenant_id="t2")
        mgr = ServerConfigManager()
        with pytest.raises(ValueError, match=r"not found in tenant"):
            await mgr.create_config(
                config=ServerConfigCreate(
                    virtual_server_id="vs-foreign",
                    name="Leaky",
                ),
                db=db,
                tenant_id="t1",
            )

    async def test_oauth_client_with_vmcp_rejected(self, db):
        # VMCPs delegate per-call auth to the underlying MCPs.
        # An oauth_client at the VMCP scope is meaningless and
        # could mislead operators — reject it loudly.
        _seed_vmcp(db, vs_id="vs-a")
        mgr = ServerConfigManager()
        with pytest.raises(ValueError, match=r"not applicable to VMCP"):
            await mgr.create_config(
                config=ServerConfigCreate(
                    virtual_server_id="vs-a",
                    oauth_client_id="oauth-x",
                    name="OAuthOnVMCP",
                ),
                db=db,
                tenant_id="t1",
            )

    async def test_duplicate_vmcp_config_name_rejected(self, db):
        # Uniqueness is scoped to (tenant, virtual_server_id, name)
        # for the VMCP path — distinct from the MCP path which
        # also keys on oauth_client_id.
        v = _seed_vmcp(db, vs_id="vs-dup", name="Dup VMCP")
        mgr = ServerConfigManager()
        await mgr.create_config(
            config=ServerConfigCreate(
                virtual_server_id=v.id,
                name="Shared Name",
            ),
            db=db,
            tenant_id="t1",
        )
        with pytest.raises(ValueError, match=r"already exists"):
            await mgr.create_config(
                config=ServerConfigCreate(
                    virtual_server_id=v.id,
                    name="Shared Name",
                ),
                db=db,
                tenant_id="t1",
            )

    async def test_vmcp_happy_path_persists_correctly(self, db):
        v = _seed_vmcp(db, vs_id="vs-ok", name="Aggregator")
        mgr = ServerConfigManager()
        result = await mgr.create_config(
            config=ServerConfigCreate(
                virtual_server_id=v.id,
                name="Aggregator Access",
                description="VMCP-backed access config",
            ),
            db=db,
            tenant_id="t1",
        )
        # Manager returns a dict shaped like the response model.
        assert result["virtual_server_id"] == v.id
        assert result["mcp_server_id"] is None
        # And the row is in the DB with the same shape.
        row = db.query(ServerConfig).filter(
            ServerConfig.id == result["id"]
        ).first()
        assert row is not None
        assert row.virtual_server_id == v.id
        assert row.mcp_server_id is None
        assert row.oauth_client_id is None
        # server_kind helper picks up the VMCP backend.
        assert row.server_kind == "vmcp"

    async def test_same_name_allowed_across_distinct_vmcps(self, db):
        # Uniqueness keys on (tenant, virtual_server_id, name) —
        # two different VMCPs can each have a config named
        # "Default" without collision.
        v1 = _seed_vmcp(db, vs_id="vs-A", name="A")
        v2 = _seed_vmcp(db, vs_id="vs-B", name="B")
        mgr = ServerConfigManager()
        r1 = await mgr.create_config(
            config=ServerConfigCreate(
                virtual_server_id=v1.id, name="Default",
            ),
            db=db, tenant_id="t1",
        )
        r2 = await mgr.create_config(
            config=ServerConfigCreate(
                virtual_server_id=v2.id, name="Default",
            ),
            db=db, tenant_id="t1",
        )
        assert r1["id"] != r2["id"]
        assert r1["virtual_server_id"] == "vs-A"
        assert r2["virtual_server_id"] == "vs-B"

    async def test_mcp_backed_path_still_works(self, db):
        # Regression — the virtual-server branch was added later; the
        # original MCP-backed path must keep working with the same inputs
        # admin clients were using before it.
        _seed_mcp(db, server_id="mcp-ok", name="Real MCP", auth_type="none")
        mgr = ServerConfigManager()
        result = await mgr.create_config(
            config=ServerConfigCreate(
                mcp_server_id="mcp-ok",
                name="Real Access",
            ),
            db=db,
            tenant_id="t1",
        )
        assert result["mcp_server_id"] == "mcp-ok"
        assert result.get("virtual_server_id") is None
        row = db.query(ServerConfig).filter(
            ServerConfig.id == result["id"]
        ).first()
        assert row.server_kind == "mcp"
