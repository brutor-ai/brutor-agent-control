# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Claude Desktop config export

Locks down `build_claude_desktop_config()` — the pure function that
turns a user's resource-group MCP access into a Claude Desktop
config dict.

# What's under test

  1. Output shape matches the `mcpServers` JSON the Claude Desktop
     config file expects (top-level `mcpServers`, each entry has
     `command: "npx"` + an `args` list with `mcp-remote` + URL +
     Authorization header).
  2. Only servers the user can actually reach (via group
     memberships) appear in the output.
  3. URL encoding: server_id ends up in the URL path; API key in
     the Authorization header — not in the URL.
  4. Empty user (no group memberships) produces empty mcpServers
     (NOT an error).
  5. Virtual servers are included alongside regular MCP servers.
  6. Cross-tenant safety — the function never returns a server
     from a different tenant even if the user happens to be a
     member of a group with the same ID elsewhere (defensive).
  7. Naming: friendly keys (`brutor-mcp-customer-crm`) are
     generated from server names with sensible slugification.
  8. Validation: empty / non-http gateway URL raises.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant
from app.database.models.virtual_server import VirtualServer
from app.services.claude_desktop.config_export import (
    build_claude_desktop_config,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            EndUser.__table__,
            ResourceGroup.__table__,
            ResourceGroupMembership.__table__,
            MCPServer.__table__,
            ServerConfig.__table__,
            VirtualServer.__table__,
            resource_group_server_configs,
            # ResourceGroup.end_user_groups selectin needs
            # these even when no test seeds an EndUserGroup row.
            EndUserGroup.__table__,
            end_user_group_members,
            resource_group_end_user_groups,
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


def _seed_user(db, *, tenant_id: str = "t1", username: str = "alice") -> EndUser:
    user = EndUser(
        tenant_id=tenant_id,
        username=username,
        email=f"{username}@example.invalid",
        password_hash="x" * 60,
    )
    db.add(user)
    db.flush()
    return user


def _seed_group(
    db, *,
    group_id: str = "grp-1",
    tenant_id: str = "t1",
    name: str = "Default",
) -> ResourceGroup:
    g = ResourceGroup(
        id=group_id, tenant_id=tenant_id, name=name,
        display_name=name,
    )
    db.add(g)
    db.flush()
    return g


def _seed_mcp_server(
    db, *,
    server_id: str,
    tenant_id: str = "t1",
    name: str = "test-server",
    enabled: bool = True,
) -> MCPServer:
    s = MCPServer(
        id=server_id, tenant_id=tenant_id, name=name,
        base_url=f"https://{server_id}.example.invalid",
        enabled=enabled,
    )
    db.add(s)
    db.flush()
    return s


def _bind_server_to_group(
    db, *,
    group: ResourceGroup,
    mcp_server: MCPServer,
    enabled: bool = True,
):
    """Create a server_config bound to a group via the M2M table."""
    cfg = ServerConfig(
        id=f"cfg-{mcp_server.id}",
        tenant_id=mcp_server.tenant_id,
        mcp_server_id=mcp_server.id,
        name=f"{mcp_server.name}-cfg",
        enabled=enabled,
        priority=0,
        is_default=False,
    )
    db.add(cfg)
    db.flush()
    db.execute(
        resource_group_server_configs.insert().values(
            resource_group_id=group.id, server_config_id=cfg.id,
        )
    )
    db.flush()
    return cfg


def _bind_vserver_to_group(
    db, *,
    group: ResourceGroup,
    virtual_server: VirtualServer,
    enabled: bool = True,
):
    """VMCP-backed server_config bound to a group.

    Mirrors `_bind_server_to_group` but sets `virtual_server_id`
    instead of `mcp_server_id` (XOR contract). SQLite test env
    doesn't enforce the CHECK constraint, which is fine — we just
    need to exercise the Python read path.
    """
    cfg = ServerConfig(
        id=f"cfg-vmcp-{virtual_server.id}",
        tenant_id=virtual_server.tenant_id,
        mcp_server_id=None,
        virtual_server_id=virtual_server.id,
        name=f"{virtual_server.name}-cfg",
        enabled=enabled,
        priority=0,
        is_default=False,
    )
    db.add(cfg)
    db.flush()
    db.execute(
        resource_group_server_configs.insert().values(
            resource_group_id=group.id, server_config_id=cfg.id,
        )
    )
    db.flush()
    return cfg


def _add_user_to_group(
    db, *,
    user: EndUser,
    group: ResourceGroup,
):
    # ResourceGroupMembership.id is auto-increment Integer; don't set it.
    m = ResourceGroupMembership(
        tenant_id=user.tenant_id,
        user_id=user.id,
        group_id=group.id,
    )
    db.add(m)
    db.flush()


# ---------------------------------------------------------------------------
# 1. Output shape + happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_output_matches_claude_desktop_schema(self, db):
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(db, server_id="mcp-customer-crm", name="Customer CRM")
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://gateway.example.com",
            api_key="sk_brutor_api_test123",
        )

        # Top-level shape.
        assert "mcpServers" in cfg
        assert isinstance(cfg["mcpServers"], dict)

        # Exactly one entry, keyed by a friendly slug.
        assert len(cfg["mcpServers"]) == 1
        key = list(cfg["mcpServers"].keys())[0]
        assert key == "brutor-mcp-customer-crm"

        entry = cfg["mcpServers"][key]
        assert entry["command"] == "npx"
        # mcp-remote bridge + URL + auth header
        assert entry["args"][0] == "-y"
        assert entry["args"][1] == "mcp-remote"
        assert entry["args"][2] == "https://gateway.example.com/v1/proxy/mcp/mcp-customer-crm"
        assert entry["args"][3] == "--header"
        assert entry["args"][4] == "Authorization: Bearer sk_brutor_api_test123"

    def test_api_key_in_header_not_url(self, db):
        # Security invariant — the API key is sensitive. URLs end up
        # in HTTP access logs everywhere along the path. It MUST be
        # in the Authorization header, NOT in the URL.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(db, server_id="mcp-a", name="A")
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://gateway.example.com",
            api_key="sk_brutor_api_SECRET",
        )
        for entry in cfg["mcpServers"].values():
            url_arg = entry["args"][2]
            assert "SECRET" not in url_arg, (
                f"API key leaked into the URL: {url_arg}"
            )
            # And it IS in the Authorization arg.
            assert "SECRET" in entry["args"][4]


# ---------------------------------------------------------------------------
# 2. Filtering by reachability
# ---------------------------------------------------------------------------


class TestReachabilityFilter:
    def test_only_reachable_servers_appear(self, db):
        user = _seed_user(db)
        group_a = _seed_group(db, group_id="grp-a", name="A")
        _add_user_to_group(db, user=user, group=group_a)
        # Server in user's group → should appear.
        reachable = _seed_mcp_server(db, server_id="mcp-reachable", name="Reachable")
        _bind_server_to_group(db, group=group_a, mcp_server=reachable)

        # Server bound to a DIFFERENT group user isn't a member of.
        group_b = _seed_group(db, group_id="grp-b", name="B")
        unreachable = _seed_mcp_server(db, server_id="mcp-unreachable", name="Unreachable")
        _bind_server_to_group(db, group=group_b, mcp_server=unreachable)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com",
            api_key="sk_x",
        )
        keys = set(cfg["mcpServers"].keys())
        assert "brutor-mcp-reachable" in keys
        assert "brutor-mcp-unreachable" not in keys

    def test_disabled_server_excluded(self, db):
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(
            db, server_id="mcp-disabled", name="Disabled", enabled=False,
        )
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert cfg["mcpServers"] == {}

    def test_user_with_no_memberships_produces_empty_config(self, db):
        # NOT an error — the user is valid, they just don't belong
        # to any group with MCP access yet. Operators see this and
        # can add the user to a group.
        user = _seed_user(db)
        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert cfg == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# 3. Virtual servers
# ---------------------------------------------------------------------------


class TestVirtualServers:
    def test_virtual_server_bound_to_group_is_included(self, db):
        # VMCPs reach the user via a VMCP-backed
        # server_config bound to one of their resource groups,
        # identical to the regular MCP path.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        v = VirtualServer(
            id="vs-aggregator", tenant_id="t1",
            name="Customer Aggregator",
            owner=str(user.id), enabled=True,
            capabilities={"tools": [], "resources": [], "prompts": []},
        )
        db.add(v)
        db.flush()
        _bind_vserver_to_group(db, group=group, virtual_server=v)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        # Key uses the brutor-vmcp- prefix so operators can tell
        # them apart from real MCP servers in the config file.
        assert "brutor-vmcp-customer-aggregator" in cfg["mcpServers"]
        entry = cfg["mcpServers"]["brutor-vmcp-customer-aggregator"]
        assert "vmcp/vs-aggregator" in entry["args"][2]

    def test_unbound_virtual_server_is_excluded(self, db):
        # VMCPs are NOT tenant-wide. A VMCP with no server_config binding must not
        # leak into any user's export.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        v = VirtualServer(
            id="vs-orphan", tenant_id="t1",
            name="Orphan VMCP",
            owner=str(user.id), enabled=True,
            capabilities={"tools": [], "resources": [], "prompts": []},
        )
        db.add(v)
        db.flush()
        # No _bind_vserver_to_group call — VMCP exists but is
        # not exposed through any server_config.

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert cfg["mcpServers"] == {}

    def test_virtual_server_bound_to_other_group_is_excluded(self, db):
        # Same reachability rule as MCPs — a VMCP bound to a
        # group the user isn't a member of must not appear.
        user = _seed_user(db)
        group_a = _seed_group(db, group_id="grp-a", name="A")
        _add_user_to_group(db, user=user, group=group_a)

        other_group = _seed_group(db, group_id="grp-b", name="B")
        v = VirtualServer(
            id="vs-other", tenant_id="t1",
            name="Other VMCP",
            owner=str(user.id), enabled=True,
            capabilities={"tools": [], "resources": [], "prompts": []},
        )
        db.add(v)
        db.flush()
        _bind_vserver_to_group(db, group=other_group, virtual_server=v)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert cfg["mcpServers"] == {}

    def test_disabled_virtual_server_excluded(self, db):
        # Mirrors the MCP disabled-server filter.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        v = VirtualServer(
            id="vs-disabled", tenant_id="t1",
            name="Disabled VMCP",
            owner=str(user.id), enabled=False,
            capabilities={"tools": [], "resources": [], "prompts": []},
        )
        db.add(v)
        db.flush()
        _bind_vserver_to_group(db, group=group, virtual_server=v)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert cfg["mcpServers"] == {}

    def test_mixed_mcp_and_vmcp_both_appear(self, db):
        # Smoke test: one MCP + one VMCP both bound to the same
        # group both surface, each under their respective prefix.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)

        srv = _seed_mcp_server(db, server_id="mcp-x", name="X Server")
        _bind_server_to_group(db, group=group, mcp_server=srv)

        v = VirtualServer(
            id="vs-y", tenant_id="t1",
            name="Y Virtual",
            owner=str(user.id), enabled=True,
            capabilities={"tools": [], "resources": [], "prompts": []},
        )
        db.add(v)
        db.flush()
        _bind_vserver_to_group(db, group=group, virtual_server=v)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        keys = set(cfg["mcpServers"].keys())
        assert "brutor-mcp-x-server" in keys
        assert "brutor-vmcp-y-virtual" in keys


# ---------------------------------------------------------------------------
# 4. Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_gateway_url_raises(self, db):
        user = _seed_user(db)
        with pytest.raises(ValueError, match="gateway_url"):
            build_claude_desktop_config(
                db=db, tenant_id="t1", user_id=user.id,
                gateway_url="", api_key="sk_x",
            )

    def test_non_http_gateway_url_raises(self, db):
        user = _seed_user(db)
        with pytest.raises(ValueError, match="http"):
            build_claude_desktop_config(
                db=db, tenant_id="t1", user_id=user.id,
                gateway_url="ftp://x.example.com",
                api_key="sk_x",
            )

    def test_unknown_user_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            build_claude_desktop_config(
                db=db, tenant_id="t1", user_id=99999,
                gateway_url="https://x.example.com",
                api_key="sk_x",
            )

    def test_cross_tenant_user_not_accessible(self, db):
        # User exists in tenant t2; request asks for t1. Must
        # behave like the user doesn't exist in t1.
        db.add(Tenant(
            id="t2", name="t2", display_name="t2",
            primary_contact_email="t2@example.invalid",
        ))
        db.flush()
        other_user = _seed_user(db, tenant_id="t2", username="bob")
        with pytest.raises(ValueError, match="not found"):
            build_claude_desktop_config(
                db=db, tenant_id="t1",  # asking against t1
                user_id=other_user.id,
                gateway_url="https://x.example.com", api_key="sk_x",
            )

    def test_gateway_url_trailing_slash_normalised(self, db):
        # Operator pastes the URL with a trailing slash — we strip
        # it so the path concatenation doesn't double up.
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(db, server_id="mcp-x", name="X")
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com/",
            api_key="sk_x",
        )
        entry = next(iter(cfg["mcpServers"].values()))
        url_arg = entry["args"][2]
        assert "//v1/" not in url_arg
        assert url_arg == "https://x.example.com/v1/proxy/mcp/mcp-x"


# ---------------------------------------------------------------------------
# 5. Slugification — friendly key generation
# ---------------------------------------------------------------------------


class TestSlugification:
    def test_server_name_with_spaces_becomes_kebab(self, db):
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(
            db, server_id="mcp-1",
            name="Customer Records Service",
        )
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        assert "brutor-mcp-customer-records-service" in cfg["mcpServers"]

    def test_special_characters_collapsed(self, db):
        user = _seed_user(db)
        group = _seed_group(db)
        _add_user_to_group(db, user=user, group=group)
        srv = _seed_mcp_server(
            db, server_id="mcp-1",
            name="Foo!@#$%^&*Bar",
        )
        _bind_server_to_group(db, group=group, mcp_server=srv)

        cfg = build_claude_desktop_config(
            db=db, tenant_id="t1", user_id=user.id,
            gateway_url="https://x.example.com", api_key="sk_x",
        )
        # Special characters become single hyphens; no double-hyphens.
        keys = list(cfg["mcpServers"].keys())
        assert len(keys) == 1
        assert "--" not in keys[0]
        assert keys[0].startswith("brutor-mcp-foo")
        assert keys[0].endswith("bar")
