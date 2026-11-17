# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# MCP tool annotations survive discovery, and snapshots record changes only

Regression suite for the 2026-09 bug: the legacy (handshake) discovery path
serialised tools through a whitelist of `name`/`description`/`inputSchema`,
so `mcp_servers.capabilities` lost every MCP `annotations` object. The core
fails closed on an unannotated tool (side-effecting), so under `restricted`
/ `approval_required` autonomy every read was blocked or held, the
`deny_tool_risk` grant constraint never matched, and evidence attributed
every call as a write.

Coverage, class by class:

  * the canonical normaliser against the SHARED cross-plane fixture
    (`brutor-gateway-core/tests/fixtures/mcp_tool_annotations_contract.json`
    — the core asserts what the runtime makes of the same stored tools);
  * BOTH discovery dialects end to end through `discover_capabilities`:
    handshake against a real in-memory MCP SDK server, stateless against
    stubbed `server/discover` + JSON-RPC lists;
  * snapshot dedupe: an unchanged re-discovery appends no snapshot (so the
    core can never blame it as a `dependency_change`), and an annotation
    flip IS recorded, flagged `is_change`, and reported with before/after.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import anyio
import mcp.types as mcp_types
import pytest
import sqlalchemy as sa
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.shared.memory import create_client_server_memory_streams
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.mcp_capability_snapshot import MCPCapabilitySnapshot
from app.database.models.mcp_server import MCPServer
from app.database.models.system_alert import SystemAlert
from app.database.models.tenant import Tenant
from app.services.mcp.capability_diff import (
    KIND_ANNOTATION_CHANGE,
    diff_capabilities,
    record_and_diff,
)
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.mcp.tool_snapshot import (
    content_hash,
    normalize_mcp_capabilities,
    normalize_tool,
    normalize_tools,
    tool_risk_attributes,
)

from tests.core_repo import core_repo_path



@pytest.fixture(scope="module")
def contract():
    # The point of the shared fixture is that both planes read the same
    # file. Skipped without BRUTOR_CORE_REPO; fails when it names a core
    # that does not have the file (`tests/core_repo.py`).
    fixture = core_repo_path() / "tests" / "fixtures" / "mcp_tool_annotations_contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _raw_tools(contract):
    return copy.deepcopy(contract["tools_list_result"]["tools"])


def _stored(contract):
    return [c["stored"] for c in contract["expected"]]


# ---------------------------------------------------------------------------
# 1. The normaliser against the shared contract
# ---------------------------------------------------------------------------


class TestNormaliserContract:
    def test_raw_tools_list_dicts_normalise_to_the_stored_contract(self, contract):
        assert normalize_tools(_raw_tools(contract)) == _stored(contract)

    def test_sdk_tool_objects_normalise_to_the_stored_contract(self, contract):
        sdk = [mcp_types.Tool.model_validate(t) for t in _raw_tools(contract)]
        assert normalize_tools(sdk) == _stored(contract)

    def test_idempotent(self, contract):
        stored = _stored(contract)
        assert normalize_tools(stored) == stored
        caps = normalize_mcp_capabilities({"tools": _raw_tools(contract), "_brutor": {"ttl_ms": 5}})
        assert normalize_mcp_capabilities(caps) == caps
        # The core's SEP-2549 freshness marker is preserved untouched.
        assert caps["_brutor"] == {"ttl_ms": 5}

    def test_python_risk_mirror_matches_the_core_contract(self, contract):
        for case in contract["expected"]:
            assert tool_risk_attributes(case["stored"].get("annotations")) == case["risk_attributes"], \
                case["stored"]["name"]

    def test_every_annotated_tool_keeps_its_annotations(self, contract):
        raw = {t["name"]: t for t in _raw_tools(contract)}
        for tool in normalize_tools(_raw_tools(contract)):
            declared = {k: v for k, v in (raw[tool["name"]].get("annotations") or {}).items()
                        if v is not None}
            assert tool.get("annotations", {}) == declared

    def test_a_nameless_tool_is_rejected(self):
        with pytest.raises(ValueError):
            normalize_tool({"description": "no name"})
        assert normalize_tools([{"description": "x"}, {"name": "ok"}]) == [
            {"name": "ok", "inputSchema": {}}
        ]

    def test_hash_ignores_order_and_volatile_metadata(self, contract):
        a = {"tools": _raw_tools(contract), "_brutor": {"fetched_epoch": 1}}
        b = {"tools": list(reversed(_raw_tools(contract))), "_brutor": {"fetched_epoch": 2}}
        assert content_hash(a) == content_hash(b)
        flipped = _raw_tools(contract)
        flipped[0]["annotations"]["readOnlyHint"] = False
        assert content_hash({"tools": flipped}) != content_hash(a)


# ---------------------------------------------------------------------------
# DB fixture (SQLite; SystemAlert.context is JSONB in production)
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    original = SystemAlert.__table__.c.context.type
    SystemAlert.__table__.c.context.type = sa.JSON()
    try:
        engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(engine, tables=[
            Tenant.__table__, MCPServer.__table__,
            MCPCapabilitySnapshot.__table__, SystemAlert.__table__,
        ])
    finally:
        SystemAlert.__table__.c.context.type = original
    session = sessionmaker(bind=engine, future=True)()
    session.add(Tenant(id="t1", name="t1", display_name="t1",
                       primary_contact_email="t1@example.invalid"))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_server(db, server_id="demo-mcp"):
    s = MCPServer(id=server_id, tenant_id="t1", name=server_id,
                  base_url=f"https://{server_id}.example.invalid", enabled=True)
    db.add(s)
    db.flush()
    return s


# ---------------------------------------------------------------------------
# 2. Both discovery dialects persist annotations
# ---------------------------------------------------------------------------


def _upstream(tools_raw):
    """A real MCP SDK server (handshake dialect) serving `tools_raw`."""
    srv = LowLevelServer("demo-applications")

    @srv.list_tools()
    async def _list_tools():
        return [mcp_types.Tool.model_validate(t) for t in tools_raw]

    return srv


def _manager_against(upstream: LowLevelServer, monkeypatch) -> MCPServerManager:
    mgr = MCPServerManager(BrutorLogger("test-mcp-tool-snapshot"))

    async def no_stateless(*_a, **_k):
        return None  # FastMCP answers only the legacy handshake

    @asynccontextmanager
    async def in_memory_session(_server, _token=None):
        async with create_client_server_memory_streams() as (client, server):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: upstream.run(
                    server[0], server[1], upstream.create_initialization_options(),
                    raise_exceptions=True,
                ))
                yield client[0], client[1], None
                tg.cancel_scope.cancel()

    monkeypatch.setattr(mgr, "_probe_stateless", no_stateless)
    monkeypatch.setattr(mgr, "_create_session_context", in_memory_session)
    monkeypatch.setattr(
        "app.services.mcp.tool_description_scanner.scan_for_poisoned_descriptions",
        lambda *_a, **_k: [],
    )
    return mgr


def test_handshake_discovery_persists_annotations(db, contract, monkeypatch):
    server = _seed_server(db)
    mgr = _manager_against(_upstream(_raw_tools(contract)), monkeypatch)

    assert asyncio.run(mgr.discover_capabilities(server=server, db=db)) is True
    db.commit()

    assert server.dialect == "handshake"
    # The whole contract, not just names — the old whitelist dropped
    # annotations / title / outputSchema / _meta here.
    assert server.capabilities["tools"] == _stored(contract)
    snap = db.query(MCPCapabilitySnapshot).one()
    assert snap.capabilities["tools"] == _stored(contract)


def test_stateless_discovery_persists_annotations(db, contract, monkeypatch):
    server = _seed_server(db)
    mgr = MCPServerManager(BrutorLogger("test-mcp-tool-snapshot"))

    async def discover(*_a, **_k):
        return {"ttlMs": 60000, "_meta": {mgr._META_SI: {"name": "demo", "version": "1"}}}

    async def rpc(_url, method, _extra, _timeout):
        return {"tools": _raw_tools(contract)} if method == "tools/list" else {}

    monkeypatch.setattr(mgr, "_probe_stateless", discover)
    monkeypatch.setattr(mgr, "_modern_rpc", rpc)

    assert asyncio.run(mgr.discover_capabilities(server=server, db=db)) is True
    db.commit()

    assert server.dialect == "stateless"
    assert server.capabilities["tools"] == _stored(contract)
    assert server.capabilities["_brutor"]["ttl_ms"] == 60000


# ---------------------------------------------------------------------------
# 3. Snapshots record changes, not refreshes
# ---------------------------------------------------------------------------


def test_rediscovering_an_unchanged_server_adds_no_snapshot(db, contract, monkeypatch):
    server = _seed_server(db)
    mgr = _manager_against(_upstream(_raw_tools(contract)), monkeypatch)
    summary: dict = {}

    asyncio.run(mgr.discover_capabilities(server=server, db=db))
    db.commit()
    asyncio.run(mgr.discover_capabilities(server=server, db=db, out_diff_summary=summary))
    db.commit()

    snaps = db.query(MCPCapabilitySnapshot).all()
    assert len(snaps) == 1, "a refresh of an unchanged server must not append a snapshot"
    # The only row is the baseline — never a dependency-change candidate.
    assert snaps[0].is_change is False
    assert snaps[0].content_hash == content_hash(server.capabilities)
    assert summary["is_empty"] is True
    assert db.query(SystemAlert).count() == 0


def test_an_annotation_flip_is_recorded_and_reported(db, contract, monkeypatch):
    server = _seed_server(db)
    tools = _raw_tools(contract)
    asyncio.run(_manager_against(_upstream(tools), monkeypatch)
                .discover_capabilities(server=server, db=db))
    db.commit()

    # record_decision now CLAIMS to be read-only — a governance bypass if
    # the upstream is lying, so it must be recorded, flagged, and loud.
    flipped = copy.deepcopy(tools)
    target = next(t for t in flipped if t["name"] == "record_decision")
    target["annotations"]["readOnlyHint"] = True
    summary: dict = {}
    asyncio.run(_manager_against(_upstream(flipped), monkeypatch)
                .discover_capabilities(server=server, db=db, out_diff_summary=summary))
    db.commit()

    snaps = (db.query(MCPCapabilitySnapshot)
             .order_by(MCPCapabilitySnapshot.snapshot_at).all())
    assert [s.is_change for s in snaps] == [False, True]
    assert summary["change_count"] == 1

    alert = db.query(SystemAlert).one()
    assert alert.severity == "high"
    change = alert.context["changes"][0]
    assert change["kind"] == KIND_ANNOTATION_CHANGE
    assert change["name"] == "record_decision"
    assert change["before"]["readOnlyHint"] is False
    assert change["after"]["readOnlyHint"] is True
    assert change["relaxes"] is True


def test_annotation_tightening_is_reported_at_medium(contract):
    old = {"tools": _stored(contract)}
    new = copy.deepcopy(old)
    new["tools"][0]["annotations"] = {"readOnlyHint": False, "destructiveHint": True}
    diff = diff_capabilities(old, new)
    assert [c.kind for c in diff.changes] == [KIND_ANNOTATION_CHANGE]
    assert diff.relaxes_risk is False


def test_record_and_diff_dedupes_direct_callers(db, contract):
    server = _seed_server(db)
    caps = {"tools": _raw_tools(contract)}
    record_and_diff(db, server, caps)
    record_and_diff(db, server, {"tools": list(reversed(_raw_tools(contract)))})
    db.commit()
    assert db.query(MCPCapabilitySnapshot).count() == 1


# ---------------------------------------------------------------------------
# 4. The admin API that feeds the Capabilities dialog keeps the fields
# ---------------------------------------------------------------------------


def test_server_list_response_keeps_annotations_and_spec_fields(db, contract):
    """Pydantic response models strip undeclared fields; `capabilities` is
    `Dict[str, Any]`, so the nested tool objects must come through whole —
    this pins it so a future typed model cannot silently drop them."""
    from app.api.v1.models.admin.mcp_server import ServerListResponse

    server = _seed_server(db)
    server.capabilities = {"tools": _raw_tools(contract)}
    db.flush()
    mgr = MCPServerManager(BrutorLogger("test-mcp-tool-snapshot"))
    payload = ServerListResponse(
        servers={server.id: mgr.to_response(server, idp_name=None)}, total=1,
    ).model_dump(mode="json")
    assert payload["servers"][server.id]["capabilities"]["tools"] == _stored(contract)
