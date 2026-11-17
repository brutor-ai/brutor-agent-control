# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the "assign Agent Skill to a group" →
"auto-bind the system Agent Skill MCP server to that group" side
effect.

Before this change, an admin who assigned an Agent Skill to a
Resource Group without also (separately) binding
`system-agent-skill-server-<tenant>` to that group ended up with a
silent runtime regression: the portal user invoking the skill got a
403 from the proxy because the group lacked the MCP server the
skill executes through. The 403 surfaced far from the admin who
caused it — the admin had no signal anything was wrong until a
user complained.

The fix lives in `_assign_skill_to_groups`: every time we (re-)assign
a skill to a set of groups, we ensure the system MCP server is bound
to each of those groups too. Idempotent (already-bound groups are
left alone). These tests pin the contract:

  * a fresh group gets the binding added
  * an already-bound group is a no-op (no duplicate rows, returned
    list is empty)
  * a tenant that's missing the system server config (legacy tenant
    from before AgentSkillServer existed) is handled gracefully —
    no exception, the skill assignment still succeeds
  * assigning a SECOND skill to the same group doesn't re-bind the
    server (idempotency holds across skills)

Run: pytest tests/test_agent_skill_auto_bind_server.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# SQLite doesn't speak JSONB. Some of the models we pull in via
# `import app.database.models` use it directly; route to plain JSON
# in the SQLite dialect so `create_all` doesn't fail.
@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


import app.database.models  # noqa: F401  — registers all tables

from app.api.v1.endpoints.admin.agent_skills import (  # noqa: E402
    _assign_skill_to_groups,
    _ensure_agent_skill_server_bound,
)
from app.constants.system_server import AgentSkillServer  # noqa: E402
from app.database import Base  # noqa: E402
from app.database.models.agent_skill import AgentSkill  # noqa: E402
from app.database.models.mcp_server import MCPServer  # noqa: E402
from app.database.models.resource_group import (  # noqa: E402
    ResourceGroup,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig  # noqa: E402
from app.database.models.tenant import Tenant  # noqa: E402


TENANT_ID = "t1"
SYS_SERVER_ID = f"{AgentSkillServer.SERVER_ID}-{TENANT_ID}"
SYS_CONFIG_ID = f"{AgentSkillServer.CONFIG_ID}-{TENANT_ID}"


# ---------------------------------------------------------------------------
# Fixture: in-memory DB with the minimum seed needed
# ---------------------------------------------------------------------------


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = SessionFactory()
    try:
        tenant = Tenant(
            id=TENANT_ID,
            name=TENANT_ID,
            display_name="T1",
            organization_name="T1",
            primary_contact_email="ops@t1.example",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
            max_admin_users=None,
        )
        setup.add(tenant)
        setup.flush()

        # `_resolve_group_ids` (called by `_assign_skill_to_groups`)
        # unconditionally resolves the tenant's Default system group
        # for the sentinel-mapping pass, even when the caller passes
        # explicit IDs. Seed it so the helper doesn't ValueError out.
        setup.add(
            ResourceGroup(
                id="rg-default",
                tenant_id=TENANT_ID,
                parent_group_id=None,
                hierarchy_level=0,
                hierarchy_path="Default",
                name="Default",
                display_name="Default",
                is_active=True,
                is_system=True,
            )
        )

        # Two non-default groups to drive assertions: one we'll assign
        # skills to ("eng"), one that stays uninvolved so test
        # assertions can scope cleanly.
        for gid, gname in [("rg-eng", "Engineering"), ("rg-other", "Other")]:
            setup.add(
                ResourceGroup(
                    id=gid,
                    tenant_id=TENANT_ID,
                    parent_group_id=None,
                    hierarchy_level=0,
                    hierarchy_path=gname,
                    name=gname,
                    display_name=gname,
                    is_active=True,
                )
            )
        setup.flush()

        setup.commit()
    finally:
        setup.close()

    yield _Harness(engine=engine, SessionFactory=SessionFactory)
    engine.dispose()


class _Harness:
    def __init__(self, engine, SessionFactory):
        self.engine = engine
        self.SessionFactory = SessionFactory

    def session(self):
        return self.SessionFactory()

    def seed_system_server(self, session):
        """Seed the tenant-scoped Agent Skill system MCP server + config.

        Mirrors the production `_seed_agent_skill_server` shape just
        enough that `_ensure_agent_skill_server_bound` can find the
        config row. We don't need icon/health/capability fields for
        the test, but the FK from ServerConfig → MCPServer must be
        valid, so seed both rows.
        """
        server = MCPServer(
            id=SYS_SERVER_ID,
            tenant_id=TENANT_ID,
            name=AgentSkillServer.NAME,
            description=AgentSkillServer.DESCRIPTION,
            base_url=AgentSkillServer.BASE_URL,
            mcp_endpoint_path=AgentSkillServer.MCP_ENDPOINT_PATH,
            auth_type=AgentSkillServer.AUTH_TYPE,
            timeout=30.0,
            enabled=True,
            capabilities={"tools": []},
        )
        session.add(server)
        session.flush()

        config = ServerConfig(
            id=SYS_CONFIG_ID,
            tenant_id=TENANT_ID,
            mcp_server_id=SYS_SERVER_ID,
            name=AgentSkillServer.NAME,
            enabled=True,
        )
        session.add(config)
        session.flush()

    def seed_skill(self, session, skill_id: str = "skill-1"):
        skill = AgentSkill(
            id=skill_id,
            tenant_id=TENANT_ID,
            name=f"{skill_id}-name",
            description="test skill",
            skill_md_content="# test",
            skill_md_hash="x",
            version="1.0.0",
            status="draft",
            enabled=True,
        )
        session.add(skill)
        session.flush()
        return skill_id

    def bindings_for(self, session, server_config_id: str):
        return [
            row.resource_group_id
            for row in session.execute(
                resource_group_server_configs.select().where(
                    resource_group_server_configs.c.server_config_id == server_config_id
                )
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# _ensure_agent_skill_server_bound — the surgical helper
# ---------------------------------------------------------------------------


class TestEnsureAgentSkillServerBound:
    def test_binds_missing_groups(self, harness):
        s = harness.session()
        try:
            harness.seed_system_server(s)
            s.commit()

            added = _ensure_agent_skill_server_bound(
                db=s,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
                created_by="admin-1",
            )
            s.commit()

            assert added == ["rg-eng"]
            assert "rg-eng" in harness.bindings_for(s, SYS_CONFIG_ID)
        finally:
            s.close()

    def test_no_op_when_already_bound(self, harness):
        s = harness.session()
        try:
            harness.seed_system_server(s)
            # Pre-bind the group ourselves.
            s.execute(
                resource_group_server_configs.insert().values(
                    id=str(uuid.uuid4()),
                    resource_group_id="rg-eng",
                    server_config_id=SYS_CONFIG_ID,
                )
            )
            s.commit()

            added = _ensure_agent_skill_server_bound(
                db=s,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
            )
            s.commit()

            # No new groups returned; binding count still 1.
            assert added == []
            bindings = harness.bindings_for(s, SYS_CONFIG_ID)
            assert bindings == ["rg-eng"]
        finally:
            s.close()

    def test_partial_overlap_only_binds_missing(self, harness):
        s = harness.session()
        try:
            harness.seed_system_server(s)
            # rg-eng already bound; rg-other not yet.
            s.execute(
                resource_group_server_configs.insert().values(
                    id=str(uuid.uuid4()),
                    resource_group_id="rg-eng",
                    server_config_id=SYS_CONFIG_ID,
                )
            )
            s.commit()

            added = _ensure_agent_skill_server_bound(
                db=s,
                group_ids=["rg-eng", "rg-other"],
                tenant_id=TENANT_ID,
            )
            s.commit()

            assert added == ["rg-other"]
            assert set(harness.bindings_for(s, SYS_CONFIG_ID)) == {
                "rg-eng",
                "rg-other",
            }
        finally:
            s.close()

    def test_returns_empty_when_tenant_lacks_system_server(self, harness):
        """Legacy tenants that haven't been re-seeded with the system
        server should NOT crash the assignment — bail out quietly so
        the skill assign call still succeeds. The admin will see a 403
        the first time a user calls the skill, surfacing the
        provisioning gap then."""
        s = harness.session()
        try:
            # Deliberately do NOT call seed_system_server here.
            added = _ensure_agent_skill_server_bound(
                db=s,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
            )
            assert added == []
        finally:
            s.close()

    def test_empty_group_list_is_a_noop(self, harness):
        s = harness.session()
        try:
            harness.seed_system_server(s)
            s.commit()

            added = _ensure_agent_skill_server_bound(
                db=s, group_ids=[], tenant_id=TENANT_ID,
            )
            assert added == []
            assert harness.bindings_for(s, SYS_CONFIG_ID) == []
        finally:
            s.close()


# ---------------------------------------------------------------------------
# _assign_skill_to_groups — wires the side effect into the public path
# ---------------------------------------------------------------------------


class TestAssignSkillToGroupsAutoBinds:
    def test_skill_assign_auto_binds_server(self, harness):
        s = harness.session()
        try:
            harness.seed_system_server(s)
            skill_id = harness.seed_skill(s)
            s.commit()

            result = _assign_skill_to_groups(
                db=s,
                skill_id=skill_id,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
                created_by="admin-1",
            )
            s.commit()

            assert result["assigned_group_ids"] == ["rg-eng"]
            assert result["auto_bound_server_group_ids"] == ["rg-eng"]
            assert harness.bindings_for(s, SYS_CONFIG_ID) == ["rg-eng"]
        finally:
            s.close()

    def test_second_skill_to_same_group_does_not_double_bind(self, harness):
        """The auto-bind side effect must stay idempotent across skills:
        assigning two skills to the same group should NOT produce two
        server-binding rows. Otherwise the rg_server_configs table
        accumulates duplicates the unique constraint would later
        reject, and the audit-log surface lies about the side effect."""
        s = harness.session()
        try:
            harness.seed_system_server(s)
            skill1 = harness.seed_skill(s, "skill-1")
            skill2 = harness.seed_skill(s, "skill-2")
            s.commit()

            first = _assign_skill_to_groups(
                db=s,
                skill_id=skill1,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
            )
            s.commit()
            assert first["auto_bound_server_group_ids"] == ["rg-eng"]

            second = _assign_skill_to_groups(
                db=s,
                skill_id=skill2,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
            )
            s.commit()
            # Second skill — server already bound by first assign, so
            # the side effect doesn't fire.
            assert second["auto_bound_server_group_ids"] == []
            # Exactly one server binding row for the group.
            assert harness.bindings_for(s, SYS_CONFIG_ID) == ["rg-eng"]
        finally:
            s.close()

    def test_skill_assign_succeeds_when_system_server_missing(self, harness):
        """Legacy-tenant safety: even without the system server, the
        skill→group assignment itself must succeed. The auto-bind
        side effect just degrades to empty."""
        s = harness.session()
        try:
            # Deliberately skip seed_system_server.
            skill_id = harness.seed_skill(s)
            s.commit()

            result = _assign_skill_to_groups(
                db=s,
                skill_id=skill_id,
                group_ids=["rg-eng"],
                tenant_id=TENANT_ID,
            )
            s.commit()

            assert result["assigned_group_ids"] == ["rg-eng"]
            assert result["auto_bound_server_group_ids"] == []
        finally:
            s.close()
