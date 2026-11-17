# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Provider Catalog — HTTP endpoint tests

In-memory SQLite + FastAPI TestClient harness (mirrors
test_llm_catalog_endpoints.py). Covers the browse-only
`GET /v1/admin/provider-catalog` route with JWT auth + AI_MODEL_READ.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.llm_provider_catalog import (
    create_llm_provider_catalog_router,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.init_llm_providers import (
    PROVIDER_SEED_CATALOG,
    seed_llm_provider_catalog,
)
from app.database.models.resource_group import GroupType, ResourceGroup
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

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    import app.database.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)

    def override_get_db():
        s = Factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test-provider-catalog")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(
        create_llm_provider_catalog_router(logger=logger), prefix="/v1"
    )
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        tid = "t1"
        s.add(Tenant(
            id=tid, name=tid, display_name="Tenant One",
            organization_name="Tenant One",
            primary_contact_email="admin@t1.example",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted",
        ))
        s.flush()
        s.add(ResourceGroup(
            id=f"group-{tid}-default", tenant_id=tid, name="Default",
            group_type=GroupType.CUSTOM, is_system=True, is_active=True,
        ))
        s.flush()
        role = TenantRole_factory(s, tid)
        auth.create_tenant_admin(
            username=f"admin_{tid}", email=f"admin@{tid}.example",
            password="Secret123!", tenant_id=tid, db=s, role_id=role.id,
        )
        # Seed the global provider catalog.
        seed_llm_provider_catalog(s, logger=logger)
        s.commit()
    finally:
        s.close()

    yield client
    app.dependency_overrides.clear()
    engine.dispose()


def TenantRole_factory(s, tid):
    from app.database.models.admin import TenantRole, TenantRolePermission
    role = TenantRole(
        id=f"role-{tid}-admin", tenant_id=tid, name="Tenant Admin",
        description="full-perm", is_system=True,
    )
    s.add(role)
    s.flush()
    for p in Permission:
        if p == Permission.SYSTEM_ADMIN:
            continue
        s.add(TenantRolePermission(
            id=f"perm-{uuid.uuid4().hex[:10]}",
            role_id=role.id, permission=p.value,
        ))
    s.flush()
    return role


def _login(client, tid="t1"):
    r = client.post(
        "/v1/admin/users/tenant/login",
        json={"username": f"admin_{tid}", "password": "Secret123!",
              "tenant_id": tid},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestProviderCatalogBrowse:
    def test_requires_auth(self, harness):
        r = harness.get("/v1/admin/provider-catalog")
        assert r.status_code in (401, 403)

    def test_browse_returns_global_catalog(self, harness):
        headers = _login(harness)
        r = harness.get("/v1/admin/provider-catalog", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == len(PROVIDER_SEED_CATALOG)
        assert len(body["providers"]) == len(PROVIDER_SEED_CATALOG)
        keys = {p["key"] for p in body["providers"]}
        assert "openai" in keys and "anthropic" in keys
        openai = next(p for p in body["providers"] if p["key"] == "openai")
        assert openai["display_name"] == "OpenAI"
        assert openai["source"] == "builtin"
        assert openai["has_icon"] is True

    def test_filter_by_category(self, harness):
        headers = _login(harness)
        r = harness.get(
            "/v1/admin/provider-catalog?category=image", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] > 0
        assert all(p["category"] == "image" for p in body["providers"])

    def test_search_substring(self, harness):
        headers = _login(harness)
        r = harness.get(
            "/v1/admin/provider-catalog?q=anthropic", headers=headers
        )
        assert r.status_code == 200, r.text
        keys = {p["key"] for p in r.json()["providers"]}
        assert keys == {"anthropic"}
