# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Identity admin API — CRUD for agent principals, grants, and resource-group
memberships. FastAPI TestClient with auth deps overridden (get_current_admin +
require_permission), SQLite session.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router
from app.core.admin_user.dependencies import get_current_admin
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.agent_identity import (
    AgentGrant, AgentIdentity, ResourceGroupAgentMember,
)
from app.database.models.agent_identity_approval_history import AgentIdentityApprovalHistory
from app.database.models.tenant import Tenant

TENANT = "t-agent"
# The tenants table is needed since the create endpoint enforces the
# max_agent_identities quota — check_quota loads the
# tenant row before counting.
# a193_approval_window_epochs — identity create/PATCH writes its approved_versions period (the one writer).
REQUIRED = [Tenant.__table__, AgentIdentity.__table__, AgentGrant.__table__, ResourceGroupAgentMember.__table__,
            AgentIdentityApprovalHistory.__table__]


class _FakeAdmin:
    id = "admin-1"
    username = "admin"
    tenant_id = TENANT

    def has_permission(self, _permission):  # noqa: D401 - test stub
        return True


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    session.add(Tenant(
        id=TENANT, name=TENANT, display_name=TENANT,
        primary_contact_email=f"{TENANT}@example.com",
        features={}, settings={},
    ))
    session.flush()

    app = FastAPI()
    app.include_router(create_agent_identities_router(BrutorLogger("t")), prefix="/v1")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_admin] = lambda: (_FakeAdmin(), "tenant")

    # require_permission(...) returns fresh closures → patch the route tree.
    def _patch(dep):
        for sub in dep.dependencies:
            if "require_permission" in getattr(sub.call, "__qualname__", ""):
                sub.call = lambda: object()
            _patch(sub)
    for route in app.routes:
        d = getattr(route, "dependant", None)
        if d:
            _patch(d)

    c = TestClient(app)
    c._session = session  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        session.close()
        engine.dispose()


def test_agent_crud_lifecycle(client):
    # create
    r = client.post("/v1/admin/agent-identities", json={"name": "billing-bot", "default_deny": True})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert aid.startswith("agent-")
    assert r.json()["status"] == "active"

    # duplicate name → 409
    assert client.post("/v1/admin/agent-identities", json={"name": "billing-bot"}).status_code == 409

    # list + get
    assert client.get("/v1/admin/agent-identities").json()["total"] == 1
    assert client.get(f"/v1/admin/agent-identities/{aid}").json()["name"] == "billing-bot"

    # patch
    r = client.patch(f"/v1/admin/agent-identities/{aid}", json={"description": "pays invoices"})
    assert r.json()["description"] == "pays invoices"

    # revoke
    r = client.post(f"/v1/admin/agent-identities/{aid}/revoke")
    assert r.json()["enabled"] is False and r.json()["status"] == "suspended"
    assert r.json()["revoked_at"] is not None


def test_grants_crud(client):
    aid = client.post("/v1/admin/agent-identities", json={"name": "bot"}).json()["id"]
    r = client.post(f"/v1/admin/agent-identities/{aid}/grants",
                    json={"action_type": "mcp_tool", "target": "shell.*", "effect": "deny"})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    assert r.json()["effect"] == "deny"
    # invalid effect rejected by model validator → 400/422 or 500-from-validator
    bad = client.post(f"/v1/admin/agent-identities/{aid}/grants",
                      json={"action_type": "mcp_tool", "effect": "maybe"})
    assert bad.status_code >= 400
    assert client.get(f"/v1/admin/agent-identities/{aid}/grants").json()["total"] == 1
    assert client.delete(f"/v1/admin/agent-identities/{aid}/grants/{gid}").status_code == 204
    assert client.get(f"/v1/admin/agent-identities/{aid}/grants").json()["total"] == 0


def test_memberships_crud(client):
    aid = client.post("/v1/admin/agent-identities", json={"name": "bot"}).json()["id"]
    r = client.post(f"/v1/admin/agent-identities/{aid}/memberships",
                    json={"group_id": "group-sales", "role": "member"})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    # idempotent re-add returns existing
    r2 = client.post(f"/v1/admin/agent-identities/{aid}/memberships", json={"group_id": "group-sales"})
    assert r2.json()["id"] == mid
    assert client.get(f"/v1/admin/agent-identities/{aid}/memberships").json()["total"] == 1
    assert client.delete(f"/v1/admin/agent-identities/{aid}/memberships/{mid}").status_code == 204
    assert client.get(f"/v1/admin/agent-identities/{aid}/memberships").json()["total"] == 0


def test_404s(client):
    assert client.get("/v1/admin/agent-identities/agent-nope").status_code == 404
    assert client.post("/v1/admin/agent-identities/agent-nope/grants",
                       json={"action_type": "llm_call", "effect": "allow"}).status_code == 404
