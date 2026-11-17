# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the system-server wire-identity stamp.

The gateway's synthesized system MCP servers (Agent Skills, Web Search)
never go through network capability discovery — the only code path that
records `protocol_version`/`dialect` on regular servers — so without a
seed-time stamp their admin-UI dialect badge stays blank forever. The
seed records the identity we know by construction: the Rust gateway is
dual-era — it answers both the legacy `initialize` handshake and the
stateless `server/discover` of MCP revision `2026-07-28` — so the badge is
`2026-07-28 · dual`.

The stamp is re-applied on every seed run (idempotent) and must not stomp
anything else on the row (seeds are create-once for user-visible state).
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant
from app.database.seed_system_servers import seed_system_mcp_servers

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    ResourceGroup.__table__,
    MCPServer.__table__,
    ServerConfig.__table__,
    resource_group_server_configs,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]

TENANT = "t-test"
GROUP = "rg-default"


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # No SQLite FK enforcement — MCPServer/ServerConfig reference admin
    # tables (identity_providers, oauth_clients) we don't load here.
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    db: Session = Factory()
    logger = BrutorLogger("test-seed-system-servers")

    db.add(
        Tenant(
            id=TENANT,
            name="test-tenant",
            display_name="Test Tenant",
            primary_contact_email="ops@example.com",
            is_default=True,
        )
    )
    db.add(
        ResourceGroup(
            id=GROUP,
            tenant_id=TENANT,
            name="Default",
            description="Default group",
        )
    )
    db.commit()

    yield db, logger
    db.close()


def _system_servers(db: Session):
    rows = (
        db.query(MCPServer)
        .filter(MCPServer.base_url.like("http://system/%"))
        .all()
    )
    assert len(rows) == 2, "expected exactly the two system servers"
    return rows


class TestWireIdentityStamp:
    def test_system_servers_stamp_dual_era(self, harness):
        """The synthesized system servers are dual-era: they answer both
        `initialize` and `server/discover`, so the badge is `2026-07-28 · dual`
        (the MCP_STATELESS_ENABLED flag that used to gate this is gone)."""
        db, logger = harness
        seed_system_mcp_servers(db, TENANT, GROUP, logger)
        db.commit()

        for server in _system_servers(db):
            assert server.protocol_version == "2026-07-28"
            assert server.dialect == "dual"

    def test_reseed_is_idempotent_and_preserves_row_state(self, harness):
        db, logger = harness
        seed_system_mcp_servers(db, TENANT, GROUP, logger)
        db.commit()

        before = {
            s.id: (s.name, s.enabled, s.capabilities) for s in _system_servers(db)
        }

        seed_system_mcp_servers(db, TENANT, GROUP, logger)
        db.commit()

        after_rows = _system_servers(db)
        after = {s.id: (s.name, s.enabled, s.capabilities) for s in after_rows}
        assert after == before, "re-seed must not stomp existing row state"
        for server in after_rows:
            assert server.dialect == "dual"


class TestRefreshCapabilitiesShortCircuit:
    """
    Admin "Refresh Capabilities" on a system server used to run network
    discovery against the synthetic http://system/... hostname and blow up
    with a DNS ExceptionGroup. `discover_capabilities` must short-circuit:
    no network I/O, wire identity re-stamped, success returned.
    """

    async def test_system_server_refresh_stamps_without_network(
        self, harness, monkeypatch
    ):
        from app.services.mcp.server.mcp_server_manager import MCPServerManager

        db, logger = harness
        seed_system_mcp_servers(db, TENANT, GROUP, logger)
        db.commit()

        manager = MCPServerManager(logger)

        async def _fail_probe(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("system server must not be probed over the network")

        monkeypatch.setattr(manager, "_probe_stateless", _fail_probe)

        for server in _system_servers(db):
            # Simulate a stale row (pre-a124 upgrade).
            server.protocol_version = None
            server.dialect = None
            db.flush()

            ok = await manager.discover_capabilities(server, db)
            assert ok is True
            assert server.protocol_version == "2026-07-28"
            assert server.dialect == "dual"
            assert server.discovered_at is not None
