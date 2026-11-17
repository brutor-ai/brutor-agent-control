# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression — /v1/admin/proxy-logs/facets must return
`display_names[field][id] = human-readable name` for ID-shaped
facets (server_id, gateway_user_id, llm_model_used). The sidebar
keeps `value` as the filter key (what /search accepts) and renders
the looked-up name for the label. Without this, operators see
opaque ULIDs in the dropdown.

server_id is union-typed in proxy_logs — the column can carry an
MCP server id, virtual server id, LLM model id, or IdP server id.
The resolver queries all four tables and merges the result.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-facets-display")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-facets-display")
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# Global compiler shim — render JSONB as plain JSON on SQLite so the
# in-memory test harness can create_all() tables that have a JSONB
# column somewhere (Tenant.data_residency, several others). The
# resolver doesn't exercise JSONB-specific operators, so the narrower
# type is functionally equivalent here. The shim only fires for the
# SQLite dialect — Postgres callers still see real JSONB.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(element, compiler, **kw):
    return "JSON"

from app.database import Base
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.tenant import Tenant
from app.database.models.virtual_server import VirtualServer
from app.services.proxy_log_service import UnifiedProxyLogService


REQUIRED_TABLES = [
    Tenant.__table__,
    MCPServer.__table__,
    VirtualServer.__table__,
    LLModel.__table__,
    IdPServer.__table__,
    EndUser.__table__,
]


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionFactory()
    # Seed one tenant; per-source-table resource records that the
    # resolver should look up.
    s.add(Tenant(
        id="t1", name="t1", display_name="Tenant One",
        organization_name="t1", primary_contact_email="a@t1.example",
        is_active=True, is_default=False, is_trial=False, suspended=False,
        billing_plan="self_hosted", max_admin_users=None,
    ))
    s.add(MCPServer(
        id="srv-mcp-1", tenant_id="t1", name="Production MCP",
        base_url="https://mcp.example", auth_type="none",
        created_at=datetime.now(timezone.utc),
    ))
    s.add(VirtualServer(
        id="srv-vmcp-1", tenant_id="t1", name="Aggregator VMCP",
        owner="alice",
        created_at=datetime.now(timezone.utc),
    ))
    s.add(LLModel(
        id="srv-llm-1", tenant_id="t1", name="Claude Opus (display)",
        model_name="claude-opus-4-7", provider_id="anthropic",
        created_at=datetime.now(timezone.utc),
    ))
    # EndUser.id is autoincrement INTEGER; proxy_logs.gateway_user_id
    # is String. SQLAlchemy auto-coerces the IN() clause via bind
    # parameter casts, so the resolver can pass facet values
    # ("1", "2") unchanged and the query still hits the right rows.
    s.add(EndUser(
        id=1, tenant_id="t1",
        username="alice", email="alice@t1.example",
        display_name="Alice Anderson",
        password_hash="x", created_at=datetime.now(timezone.utc),
    ))
    s.add(EndUser(
        id=2, tenant_id="t1",
        username="bob", email="bob@t1.example",
        display_name=None,  # falls back to username
        password_hash="x", created_at=datetime.now(timezone.utc),
    ))
    s.add(EndUser(
        id=3, tenant_id="t1",
        # username is NOT NULL on the column, but "" is falsy in the
        # resolver's `display or username or email` chain so this
        # exercises the email fallback path.
        username="", email="charlie@t1.example",
        display_name=None,
        password_hash="x", created_at=datetime.now(timezone.utc),
    ))
    # Cross-tenant control row — must NOT leak into tenant 1's labels.
    # MCPServer.id is globally unique (PK), so we use a different id
    # and rely on the resolver's tenant_id filter — not on id
    # collision — to assert isolation.
    s.add(Tenant(
        id="t2", name="t2", display_name="Tenant Two",
        organization_name="t2", primary_contact_email="a@t2.example",
        is_active=True, is_default=False, is_trial=False, suspended=False,
        billing_plan="self_hosted", max_admin_users=None,
    ))
    s.add(MCPServer(
        id="srv-mcp-tenant2",
        tenant_id="t2", name="OTHER TENANT MCP",
        base_url="https://other.example", auth_type="none",
        created_at=datetime.now(timezone.utc),
    ))
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def service():
    # The service's only required ctor arg is logger; pass a no-op.
    from app.core.logging_system import BrutorLogger
    return UnifiedProxyLogService(logger=BrutorLogger("test"))


class TestResolveDisplayNames:
    def test_server_id_resolves_to_mcp_server_name(self, service, session):
        facets = {"server_id": [{"value": "srv-mcp-1", "count": 5}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["server_id"]["srv-mcp-1"] == "Production MCP"

    def test_server_id_resolves_virtual_server_name(self, service, session):
        facets = {"server_id": [{"value": "srv-vmcp-1", "count": 3}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["server_id"]["srv-vmcp-1"] == "Aggregator VMCP"

    def test_server_id_resolves_llm_model_name(self, service, session):
        facets = {"server_id": [{"value": "srv-llm-1", "count": 8}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["server_id"]["srv-llm-1"] == "Claude Opus (display)"

    def test_server_id_merges_across_source_tables(self, service, session):
        # The same /facets call can return IDs from different source
        # tables (proxy_type spans MCP + LLM + VMCP). The resolver
        # must collapse them into a single field map.
        facets = {"server_id": [
            {"value": "srv-mcp-1", "count": 5},
            {"value": "srv-vmcp-1", "count": 3},
            {"value": "srv-llm-1", "count": 8},
        ]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["server_id"] == {
            "srv-mcp-1": "Production MCP",
            "srv-vmcp-1": "Aggregator VMCP",
            "srv-llm-1": "Claude Opus (display)",
        }

    def test_server_id_lookup_is_tenant_scoped(self, service, session):
        # Tenant 2 has its own MCP server. Asking for tenant 1's
        # facets must NOT resolve tenant 2's server id at all.
        facets = {"server_id": [{"value": "srv-mcp-tenant2", "count": 5}]}
        result = service.resolve_display_names(session, "t1", facets)
        # Either no server_id key, or the t2 id is omitted from the
        # field map (which the UI will then render as the raw value).
        assert "srv-mcp-tenant2" not in (result.get("server_id") or {})

    def test_gateway_user_id_prefers_username(self, service, session):
        # Deliberate behaviour (proxy_log_service.resolve_display_names):
        # the label prefers `username` so the facet matches the proxy-log
        # "Gateway User" column (which shows gateway_user_username). Alice
        # has BOTH a username and a display_name; the username wins.
        facets = {"gateway_user_id": [{"value": "1", "count": 4}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["gateway_user_id"]["1"] == "alice"

    def test_gateway_user_id_falls_back_to_username(self, service, session):
        # Bob has no display_name. Resolver falls back to username.
        facets = {"gateway_user_id": [{"value": "2", "count": 2}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["gateway_user_id"]["2"] == "bob"

    def test_gateway_user_id_falls_back_to_email(self, service, session):
        # Charlie has neither display_name nor username. Resolver
        # falls back to email — the dropdown is never blank.
        facets = {"gateway_user_id": [{"value": "3", "count": 1}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["gateway_user_id"]["3"] == "charlie@t1.example"

    def test_unknown_id_drops_from_map(self, service, session):
        # An id that doesn't exist in any source table is silently
        # omitted — the UI then renders the raw value, which is the
        # right fallback (something is better than nothing).
        facets = {"server_id": [
            {"value": "srv-mcp-1", "count": 5},
            {"value": "srv-missing", "count": 1},
        ]}
        result = service.resolve_display_names(session, "t1", facets)
        assert "srv-mcp-1" in result["server_id"]
        assert "srv-missing" not in result["server_id"]

    def test_empty_facets_returns_empty_map(self, service, session):
        result = service.resolve_display_names(session, "t1", {})
        assert result == {}

    def test_field_without_resolvable_values_is_omitted(self, service, session):
        # http_status_code isn't ID-shaped — the resolver shouldn't
        # invent a key for it. Keeps the response payload minimal.
        facets = {"http_status_code": [{"value": 200, "count": 50}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert "http_status_code" not in result

    def test_llm_model_used_resolves_to_model_display_name(self, service, session):
        # llm_model_used uses LLModel.name (the user-set display
        # name), NOT model_name (the canonical "claude-opus-4-7"
        # provider string).
        facets = {"llm_model_used": [{"value": "srv-llm-1", "count": 8}]}
        result = service.resolve_display_names(session, "t1", facets)
        assert result["llm_model_used"]["srv-llm-1"] == "Claude Opus (display)"


class TestResolverIsFailSoft:
    """Production observed dirty data — proxy_logs.gateway_user_id
    has carried non-integer values (resource group IDs, etc.) for
    rows where there is no end_users row. EndUser.id is Integer, so
    `.in_(['2', 'group-…', '3'])` makes Postgres reject the whole
    query and 500s /facets. These tests pin the fail-soft behaviour
    that lets the resolver keep working through that."""

    def test_non_integer_gateway_user_ids_are_skipped(self, service, session):
        # The exact production case: mixed integer-strings and ULIDs
        # in gateway_user_id facet values. The resolver must filter
        # to int-parseable values before querying EndUser, return
        # whatever int IDs do resolve, and not raise.
        facets = {
            "gateway_user_id": [
                {"value": "1", "count": 5},
                {"value": "group-01KT9QMW826ZXCDDK3EG405Q1G", "count": 3},
                {"value": "2", "count": 2},
            ],
        }
        result = service.resolve_display_names(session, "t1", facets)
        mapping = result["gateway_user_id"]
        # The two int-shaped IDs resolve; the ULID is silently
        # dropped (UI falls back to rendering the raw value).
        assert "1" in mapping
        assert "2" in mapping
        assert "group-01KT9QMW826ZXCDDK3EG405Q1G" not in mapping

    def test_all_garbage_gateway_user_ids_returns_no_mapping(self, service, session):
        # When nothing is int-parseable, the resolver simply returns
        # no gateway_user_id key. Still doesn't raise.
        facets = {
            "gateway_user_id": [
                {"value": "group-a", "count": 5},
                {"value": "key-b", "count": 3},
                {"value": "", "count": 2},  # empty
                {"value": None, "count": 1},  # None
            ],
        }
        result = service.resolve_display_names(session, "t1", facets)
        assert "gateway_user_id" not in result

    def test_one_bad_field_does_not_block_others(self, service, session):
        # If the gateway_user_id lookup somehow fails entirely, the
        # server_id and llm_model_used resolutions must still flow
        # through. Mix valid data for those with all-garbage user IDs
        # — should get partial results, not a 500.
        facets = {
            "server_id": [{"value": "srv-mcp-1", "count": 5}],
            "gateway_user_id": [
                {"value": "group-01KT9QMW826ZXCDDK3EG405Q1G", "count": 3},
            ],
            "llm_model_used": [{"value": "srv-llm-1", "count": 8}],
        }
        result = service.resolve_display_names(session, "t1", facets)
        assert result["server_id"]["srv-mcp-1"] == "Production MCP"
        assert result["llm_model_used"]["srv-llm-1"] == "Claude Opus (display)"
        assert "gateway_user_id" not in result
