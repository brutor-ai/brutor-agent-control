# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
API key ↔ agent-identity binding — the admin API surface for actor
attribution.

The proxy attributes a request to an agent principal via
`api_keys.agent_id` (core: auth.rs sets `actor_agent_id` from the key),
but until this endpoint existed nothing in the admin API could WRITE the
column — demos bound it with direct SQL. These tests pin the new surface:

- POST /{group}/api-keys accepts `agent_id`, validates it in-tenant (404
  on unknown), and echoes it on the created-response.
- GET /{group}/api-keys summaries carry `agent_id` (the recurring
  Pydantic field-drop class).
- PATCH /api-keys/{key_id} rebinds, unbinds via `agent_id: ""`, 404s on
  an unknown agent, and 400s on a revoked key.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.models.admin.resource_group import (
    APIKeyCreate, APIKeyCreatedResponse, APIKeySummary, APIKeyUpdate,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import TenantRole, TenantRolePermission
from app.database.models.agent_identity import AgentIdentity
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]


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

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(
        create_resource_groups_router(usage_limit_enforcer=MagicMock(), logger=logger),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        setup.add(Tenant(
            id="t1", name="t1", display_name="T1",
            primary_contact_email="a@t1.invalid",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted",
        ))
        setup.flush()
        role = TenantRole(
            id="role-t1-admin", tenant_id="t1", name="Tenant Admin",
            description="full", is_system=True,
        )
        setup.add(role)
        setup.flush()
        for p in Permission:
            if p == Permission.SYSTEM_ADMIN:
                continue
            setup.add(TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=role.id, permission=p.value,
            ))
        auth.create_tenant_admin(
            username="adm1", email="adm1@t1.example", password="Secret123!",
            tenant_id="t1", db=setup, role_id="role-t1-admin",
        )
        setup.add(ResourceGroup(
            id="rg-agents", tenant_id="t1", name="Agents",
            display_name="Agents", hierarchy_level=0, is_active=True,
        ))
        setup.add(AgentIdentity(
            id="agent-worker", tenant_id="t1", name="Data Export Worker",
            source="manual", default_deny=True, enforcement_mode="enforce",
        ))
        setup.add(AgentIdentity(
            id="agent-other", tenant_id="t1", name="Other Agent",
            source="manual", default_deny=True, enforcement_mode="enforce",
        ))
        # An agent in ANOTHER tenant — binding to it must 404, not leak.
        setup.add(Tenant(
            id="t2", name="t2", display_name="T2",
            primary_contact_email="a@t2.invalid",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted",
        ))
        setup.add(AgentIdentity(
            id="agent-foreign", tenant_id="t2", name="Foreign",
            source="manual", default_deny=True, enforcement_mode="enforce",
        ))
        setup.commit()
    finally:
        setup.close()

    class _H:
        def __init__(self):
            self.client = client
            self.SessionFactory = SessionFactory

        def login(self):
            r = client.post(
                "/v1/admin/users/tenant/login",
                json={"username": "adm1", "password": "Secret123!", "tenant_id": "t1"},
            )
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        def create_key(self, headers, name="worker-key", agent_id=None,
                       expect=201):
            body = {"name": name, "access_mode": "shared"}
            if agent_id is not None:
                body["agent_id"] = agent_id
            r = client.post("/v1/admin/resource-groups/rg-agents/api-keys",
                            headers=headers, json=body)
            assert r.status_code == expect, r.text
            return r.json()

    yield _H()
    app.dependency_overrides.clear()
    engine.dispose()


def test_models_declare_agent_id():
    """Regression guard against the Pydantic field-drop class: every key
    model on the wire must declare agent_id or it silently disappears."""
    for model in (APIKeyCreate, APIKeyUpdate, APIKeyCreatedResponse, APIKeySummary):
        assert "agent_id" in model.model_fields, model.__name__


def test_create_key_bound_to_agent(harness):
    headers = harness.login()
    created = harness.create_key(headers, agent_id="agent-worker")
    assert created["agent_id"] == "agent-worker"
    assert created["key_value"].startswith("sk_")

    # The group listing carries the binding (field-by-field builder path).
    r = harness.client.get("/v1/admin/resource-groups/rg-agents/api-keys",
                           headers=headers)
    assert r.status_code == 200, r.text
    rows = {k["name"]: k for k in r.json()["keys"]}
    assert rows["worker-key"]["agent_id"] == "agent-worker"


def test_create_key_unknown_agent_404(harness):
    headers = harness.login()
    harness.create_key(headers, name="bad", agent_id="agent-nope", expect=404)


def test_create_key_foreign_tenant_agent_404(harness):
    """Cross-tenant agent ids must be indistinguishable from unknown ones."""
    headers = harness.login()
    harness.create_key(headers, name="bad2", agent_id="agent-foreign", expect=404)


def test_patch_rebind_unbind_and_validation(harness):
    headers = harness.login()
    created = harness.create_key(headers, agent_id="agent-worker")
    key_id = created["id"]

    # Rebind to another agent.
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"agent_id": "agent-other"})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "agent-other"

    # Unknown agent → 404, binding unchanged.
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"agent_id": "agent-nope"})
    assert r.status_code == 404, r.text

    # Empty string unbinds.
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"agent_id": ""})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] is None

    # Omitting the field leaves the binding untouched (rebind, then rename).
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"agent_id": "agent-worker"})
    assert r.status_code == 200
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"name": "renamed-key"})
    assert r.status_code == 200, r.text
    r = harness.client.get("/v1/admin/resource-groups/rg-agents/api-keys",
                           headers=headers)
    rows = {k["name"]: k for k in r.json()["keys"]}
    assert rows["renamed-key"]["agent_id"] == "agent-worker"


def test_patch_revoked_key_400(harness):
    headers = harness.login()
    created = harness.create_key(headers, name="doomed", agent_id=None)
    key_id = created["id"]
    r = harness.client.post(f"/v1/admin/resource-groups/api-keys/{key_id}/revoke",
                            headers=headers, json={"reason": "test"})
    assert r.status_code == 200, r.text
    r = harness.client.patch(f"/v1/admin/resource-groups/api-keys/{key_id}",
                             headers=headers, json={"agent_id": "agent-worker"})
    assert r.status_code == 400, r.text
