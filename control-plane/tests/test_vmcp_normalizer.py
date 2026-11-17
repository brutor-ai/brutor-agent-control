# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# VMCP capabilities normalizer

Locks down `normalize_capabilities()` — the function that turns the
admin's stated intent (which upstream tools to expose, optional
rename/prefix/description-override) into the normalized JSON shape
the Rust proxy reads on every `tools/list` and `tools/call`.

# What's under test

  1. Pass-through of basic refs (the old shape continues to work).
  2. `tool_name_prefix` applies to every tool that doesn't have an
     explicit `exposed_name` — explicit wins, prefix is a fallback.
  3. Explicit `exposed_name` overrides both prefix and capability_name.
  4. `description_override` round-trips when provided, is absent
     otherwise.
  5. `tool_def` is snapshotted from the upstream MCPServer.capabilities
     so the Rust list handler returns proper MCP tool objects.
  6. `source_mapping` is keyed by EXPOSED name (not capability_name)
     and maps to the upstream server_id — the lookup key the Rust
     `tools/call` handler uses.
  7. `reverse_name_mapping` maps exposed → capability_name so the
     Rust handler can rewrite `params.name` before delegating to the
     upstream.
  8. Collision detection — two entries that resolve to the same
     exposed_name raise VMCPNormalizationError naming both servers.
  9. Missing upstream — orphan refs raise rather than silently
     creating a VMCP that 404s at runtime.
 10. Idempotency — re-normalizing already-normalized JSON yields the
     same result (modulo refreshed snapshots).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.mcp_server import MCPServer
from app.database.models.tenant import Tenant
from app.services.mcp.server.vmcp_normalizer import (
    normalize_capabilities,
    VMCPNormalizationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """In-memory SQLite with just the tables this normalizer touches."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine, tables=[Tenant.__table__, MCPServer.__table__],
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


def _make_upstream(
    db, *, server_id: str, tools: list[dict] | None = None,
    resources: list[dict] | None = None,
) -> MCPServer:
    """Insert an upstream MCPServer with a capabilities snapshot."""
    capabilities = {}
    if tools is not None:
        capabilities["tools"] = tools
    if resources is not None:
        capabilities["resources"] = resources
    server = MCPServer(
        id=server_id,
        tenant_id="t1",
        name=server_id,
        base_url=f"https://{server_id}.example.invalid",
        capabilities=capabilities or None,
    )
    db.add(server)
    db.flush()
    return server


# ---------------------------------------------------------------------------
# 1. Pass-through + prefix
# ---------------------------------------------------------------------------


class TestBasicNormalization:
    def test_empty_input_yields_empty_normalized(self, db):
        result = normalize_capabilities(
            db=db, tenant_id="t1", capabilities_input={},
        )
        assert result["tools"] == []
        assert result["resources"] == []
        assert result["prompts"] == []
        assert result["source_mapping"] == {}
        assert result["reverse_name_mapping"] == {}

    def test_basic_tool_ref_preserved(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "Search the index",
             "inputSchema": {"type": "object"}},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
            },
        )
        assert len(result["tools"]) == 1
        entry = result["tools"][0]
        assert entry["server_id"] == "srv-a"
        assert entry["capability_name"] == "search"
        assert entry["exposed_name"] == "search"  # no override, no prefix
        assert entry["tool_def"]["name"] == "search"
        assert entry["tool_def"]["description"] == "Search the index"

    def test_source_mapping_keyed_by_exposed_name(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
            },
        )
        assert result["source_mapping"] == {"search": "srv-a"}
        assert result["reverse_name_mapping"] == {"search": "search"}


# ---------------------------------------------------------------------------
# 2. Prefix + per-tool override
# ---------------------------------------------------------------------------


class TestRenameAndPrefix:
    def test_prefix_applies_to_unrenamed_tools(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
            {"name": "fetch", "description": "y"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tool_name_prefix": "acme_",
                "tools": [
                    {"server_id": "srv-a", "capability_name": "search"},
                    {"server_id": "srv-a", "capability_name": "fetch"},
                ],
            },
        )
        names = {t["exposed_name"] for t in result["tools"]}
        assert names == {"acme_search", "acme_fetch"}
        # Reverse mapping must let the Rust handler unrewrite to the
        # original capability_name before delegating.
        assert result["reverse_name_mapping"]["acme_search"] == "search"
        assert result["reverse_name_mapping"]["acme_fetch"] == "fetch"

    def test_explicit_exposed_name_wins_over_prefix(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tool_name_prefix": "acme_",
                "tools": [{
                    "server_id": "srv-a",
                    "capability_name": "search",
                    "exposed_name": "company_search",
                }],
            },
        )
        assert result["tools"][0]["exposed_name"] == "company_search"
        # Prefix is NOT applied on top of explicit override.
        assert "acme_company_search" not in result["source_mapping"]
        assert result["source_mapping"] == {"company_search": "srv-a"}

    def test_explicit_exposed_name_with_no_prefix(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{
                    "server_id": "srv-a",
                    "capability_name": "search",
                    "exposed_name": "lookup",
                }],
            },
        )
        assert result["tools"][0]["exposed_name"] == "lookup"
        assert result["reverse_name_mapping"]["lookup"] == "search"


# ---------------------------------------------------------------------------
# 3. Description override
# ---------------------------------------------------------------------------


class TestDescriptionOverride:
    def test_description_override_round_trips_when_provided(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "Original upstream desc"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{
                    "server_id": "srv-a",
                    "capability_name": "search",
                    "description_override": "Tenant-curated description",
                }],
            },
        )
        assert result["tools"][0]["description_override"] == (
            "Tenant-curated description"
        )

    def test_description_override_absent_when_not_provided(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
            },
        )
        assert "description_override" not in result["tools"][0]


# ---------------------------------------------------------------------------
# 4. tool_def snapshotting
# ---------------------------------------------------------------------------


class TestUpstreamSnapshot:
    def test_tool_def_snapshot_carries_full_upstream_shape(self, db):
        # The Rust list handler needs the upstream's full tool def
        # (name, description, inputSchema) to return a proper MCP
        # tool to clients — not a {server_id, capability_name} ref.
        _make_upstream(db, server_id="srv-a", tools=[
            {
                "name": "search",
                "description": "Search the index",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
            },
        )
        tool_def = result["tools"][0]["tool_def"]
        assert tool_def["name"] == "search"
        assert tool_def["inputSchema"]["required"] == ["q"]

    def test_missing_upstream_capability_logs_but_does_not_fail(self, db, caplog):
        # Upstream exists but doesn't list this tool — we still
        # save the ref so the operator can keep the VMCP working
        # when the upstream's capability list re-discovers.
        import logging
        caplog.set_level(logging.WARNING, logger="brutor-vmcp-normalizer")
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "other_tool", "description": "x"},
        ])
        result = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
            },
        )
        assert len(result["tools"]) == 1
        assert "tool_def" not in result["tools"][0]
        # The normalizer warns so operators can see the drift in logs.
        assert any(
            "tool def not in capability snapshot" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 5. Collision detection
# ---------------------------------------------------------------------------


class TestCollisionDetection:
    def test_two_tools_same_exposed_name_via_rename_rejected(self, db):
        # Two upstreams both have a 'search' tool; operator renames
        # both to the same exposed_name. Drift would cause one to
        # silently shadow the other.
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        _make_upstream(db, server_id="srv-b", tools=[
            {"name": "fetch", "description": "y"},
        ])
        with pytest.raises(VMCPNormalizationError, match="claimed by two entries"):
            normalize_capabilities(
                db=db, tenant_id="t1",
                capabilities_input={
                    "tools": [
                        {"server_id": "srv-a", "capability_name": "search",
                         "exposed_name": "lookup"},
                        {"server_id": "srv-b", "capability_name": "fetch",
                         "exposed_name": "lookup"},
                    ],
                },
            )

    def test_two_upstreams_same_tool_name_no_override_rejected(self, db):
        # The "two upstreams both have a `search` tool" case Obot
        # specifically calls out. Without an override the normalizer
        # MUST refuse — otherwise tools/call routing breaks.
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        _make_upstream(db, server_id="srv-b", tools=[
            {"name": "search", "description": "y"},
        ])
        with pytest.raises(VMCPNormalizationError, match="claimed by two entries"):
            normalize_capabilities(
                db=db, tenant_id="t1",
                capabilities_input={
                    "tools": [
                        {"server_id": "srv-a", "capability_name": "search"},
                        {"server_id": "srv-b", "capability_name": "search"},
                    ],
                },
            )

    def test_collision_error_names_both_servers(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        _make_upstream(db, server_id="srv-b", tools=[
            {"name": "search", "description": "y"},
        ])
        with pytest.raises(VMCPNormalizationError) as ei:
            normalize_capabilities(
                db=db, tenant_id="t1",
                capabilities_input={
                    "tools": [
                        {"server_id": "srv-a", "capability_name": "search"},
                        {"server_id": "srv-b", "capability_name": "search"},
                    ],
                },
            )
        msg = str(ei.value)
        assert "srv-a" in msg and "srv-b" in msg


# ---------------------------------------------------------------------------
# 6. Missing upstream
# ---------------------------------------------------------------------------


class TestMissingUpstream:
    def test_orphan_server_ref_rejected(self, db):
        # Referenced upstream doesn't exist in this tenant — would
        # otherwise produce a VMCP that 404s on tools/list at the
        # Rust handler. Catch it at save time instead.
        with pytest.raises(VMCPNormalizationError, match="not found in tenant"):
            normalize_capabilities(
                db=db, tenant_id="t1",
                capabilities_input={
                    "tools": [{"server_id": "srv-nonexistent",
                               "capability_name": "search"}],
                },
            )

    def test_cross_tenant_upstream_ref_rejected(self, db):
        # Upstream belongs to tenant B but VMCP is in tenant A —
        # the normalizer scopes the lookup by tenant_id so this
        # silently fails the "not found" branch (which is what
        # we want — no cross-tenant data leak).
        # Insert a second tenant + upstream
        db.add(Tenant(
            id="t2", name="t2", display_name="t2",
            primary_contact_email="t2@example.invalid",
        ))
        db.flush()
        db.add(MCPServer(
            id="srv-other",
            tenant_id="t2",
            name="srv-other",
            base_url="https://x.invalid",
            capabilities={"tools": [{"name": "search", "description": "x"}]},
        ))
        db.flush()
        with pytest.raises(VMCPNormalizationError, match="not found in tenant"):
            normalize_capabilities(
                db=db, tenant_id="t1",  # querying as t1
                capabilities_input={
                    "tools": [{"server_id": "srv-other",
                               "capability_name": "search"}],
                },
            )


# ---------------------------------------------------------------------------
# 7. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_re_normalizing_normalized_json_is_stable(self, db):
        _make_upstream(db, server_id="srv-a", tools=[
            {"name": "search", "description": "x"},
        ])
        first = normalize_capabilities(
            db=db, tenant_id="t1",
            capabilities_input={
                "tools": [{"server_id": "srv-a", "capability_name": "search"}],
                "tool_name_prefix": "acme_",
            },
        )
        # Feed the already-normalized JSON back in.
        second = normalize_capabilities(
            db=db, tenant_id="t1", capabilities_input=first,
        )
        # source_mapping and reverse_name_mapping should match
        # exactly — the operator's intent didn't change, so the
        # derived maps shouldn't either.
        assert first["source_mapping"] == second["source_mapping"]
        assert first["reverse_name_mapping"] == second["reverse_name_mapping"]
        # tool_def is snapshotted and may refresh; check structural shape.
        assert {t["exposed_name"] for t in first["tools"]} == \
               {t["exposed_name"] for t in second["tools"]}
