# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog — HTTP endpoint tests

In-memory SQLite + FastAPI TestClient harness (mirrors
test_ai_assets_endpoints.py). Covers browse/search/get/import with JWT
auth + perms; import returns a configured LLModelDetailResponse with
catalog_id; tenant scoping; AI_MODEL_CREATE enforcement on import.
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
from app.api.v1.endpoints.admin.llm_catalog import create_llm_catalog_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from unittest.mock import MagicMock

from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.app_factory import harness_extras


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

    # Import the full model registry so every table + association table the
    # ResourceGroup relationship graph touches exists (the import response
    # eager-loads resource_groups, which joins end_user_groups etc.).
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

    enc = MagicMock()
    app = FastAPI()
    logger = BrutorLogger("test-llm-catalog")
    manager = LLModelsManager(logger=logger, encryption_service=enc)
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(logger):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(
        create_llm_catalog_router(
            model_manager=manager, logger=logger, encryption_service=enc
        ),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        harness_extras().seed_principals(auth, s, 'Secret123!')
        for tid, name in [("t1", "Tenant One"), ("t2", "Tenant Two")]:
            s.add(Tenant(
                id=tid, name=tid, display_name=name,
                organization_name=name,
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
            ))
            s.flush()
            # Default system resource group — resolve_default_group_id
            # requires it, and import assigns models to it.
            s.add(ResourceGroup(
                id=f"group-{tid}-default", tenant_id=tid, name="Default",
                group_type=GroupType.CUSTOM, is_system=True, is_active=True,
            ))
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin", tenant_id=tid,
                name="Tenant Admin", description="full-perm",
                is_system=True,
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
            auth.create_tenant_admin(
                username=f"admin_{tid}", email=f"admin@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=role.id,
            )
            # Read-only role (AI_MODEL_READ but NOT AI_MODEL_CREATE).
            ro_role = TenantRole(
                id=f"role-{tid}-ro", tenant_id=tid,
                name="Model Reader", description="read-only",
                is_system=False,
            )
            s.add(ro_role)
            s.flush()
            s.add(TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=ro_role.id, permission=Permission.AI_MODEL_READ.value,
            ))
            auth.create_tenant_admin(
                username=f"reader_{tid}", email=f"reader@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=ro_role.id,
            )
        # Seed the global catalog.
        s.add_all([
            LlmCatalog(id="llmcat-openai-gpt-4o", provider_key="openai",
                       model_name="gpt-4o", display_name="GPT-4o",
                       mode="chat", recommended=True,
                       default_cost_per_input_token=0.0025,
                       capabilities={"supports_vision": True}),
            LlmCatalog(id="llmcat-anthropic-claude", provider_key="anthropic",
                       model_name="claude-sonnet-4-5",
                       display_name="Claude Sonnet 4.5", mode="chat",
                       recommended=True),
            LlmCatalog(id="llmcat-openai-emb", provider_key="openai",
                       model_name="text-embedding-3-small",
                       display_name="Embed Small", mode="embedding"),
        ])
        s.commit()
    finally:
        s.close()

    yield _H(client=client, Factory=Factory)
    app.dependency_overrides.clear()
    engine.dispose()


class _H:
    def __init__(self, client, Factory):
        self.client = client
        self.Factory = Factory

    def login(self, tenant_id: str) -> dict:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": f"admin_{tenant_id}", "password": "Secret123!",
                  "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def login_reader(self, tenant_id: str) -> dict:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": f"reader_{tenant_id}", "password": "Secret123!",
                  "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def login_platform_admin(self) -> dict:
        """The administrator of the installation: who may write what every
        tenant shares (`require_platform_admin`). Asked of the principal
        registry, as the route does."""
        from app.composition.principals import PRINCIPALS

        if any(PRINCIPALS.get(name).platform_wide for name in PRINCIPALS.names()):
            return self.login_system()
        return self.login("t1")

    def login_system(self) -> dict:
        r = self.client.post(
            "/v1/admin/users/system/login",
            json={"username": "sysadmin", "password": "Secret123!"},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


# =============================================================================
# Browse / search / get
# =============================================================================


class TestBrowse:
    def test_list_all(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/llm-catalog", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 3
        assert len(body["catalog"]) == 3

    def test_filter_provider(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/llm-catalog?provider_key=anthropic", headers=h
        )
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_filter_mode(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/llm-catalog?mode=embedding", headers=h
        )
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_filter_recommended(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/llm-catalog?recommended=true", headers=h
        )
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_q_search(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/llm-catalog?q=sonnet", headers=h)
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_get_entry(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o", headers=h
        )
        assert r.status_code == 200
        assert r.json()["model_name"] == "gpt-4o"

    def test_get_missing_404(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/llm-catalog/nope", headers=h)
        assert r.status_code == 404

    def test_bad_mode_400(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/llm-catalog?mode=bogus", headers=h)
        assert r.status_code == 400


# =============================================================================
# Import
# =============================================================================


class TestImport:
    def test_import_creates_configured_model(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o/import",
            headers=h, json={"group_ids": []},
        )
        assert r.status_code == 201, r.text
        model = r.json()["model"]
        assert model["id"].startswith("llm-")
        assert model["catalog_id"] == "llmcat-openai-gpt-4o"
        assert model["model_name"] == "gpt-4o"
        assert model["enabled"] is True
        assert model["provider"] == "openai"

    def test_import_empty_body_ok(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-anthropic-claude/import", headers=h,
        )
        assert r.status_code == 201, r.text
        assert r.json()["model"]["model_name"] == "claude-sonnet-4-5"

    def test_import_overrides(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o/import",
            headers=h, json={"name": "My GPT", "enabled": False,
                             "group_ids": []},
        )
        assert r.status_code == 201, r.text
        model = r.json()["model"]
        assert model["name"] == "My GPT"
        assert model["enabled"] is False

    def test_import_missing_entry_404(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-nope/import",
            headers=h, json={"group_ids": []},
        )
        assert r.status_code == 404


# =============================================================================
# Tenant scope + permissions
# =============================================================================


class TestTenantScope:
    def test_import_lands_in_correct_tenant(self, harness):
        h1 = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o/import",
            headers=h1, json={"group_ids": []},
        )
        assert r.status_code == 201
        model_id = r.json()["model"]["id"]
        s = harness.Factory()
        try:
            row = s.get(LLModel, model_id)
            assert row.tenant_id == "t1"
        finally:
            s.close()


class TestPermissions:
    def test_reader_can_browse(self, harness):
        h = harness.login_reader("t1")
        r = harness.client.get("/v1/admin/llm-catalog", headers=h)
        assert r.status_code == 200

    def test_reader_cannot_import(self, harness):
        h = harness.login_reader("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o/import",
            headers=h, json={"group_ids": []},
        )
        assert r.status_code == 403


# =============================================================================
# Curation (system-admin only) — create / update / delete
# =============================================================================


class TestCurationTenantAdminForbidden:


    def test_tenant_admin_can_still_browse(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/llm-catalog", headers=h)
        assert r.status_code == 200

    def test_tenant_admin_can_still_import(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/llm-catalog/llmcat-anthropic-claude/import",
            headers=h, json={"group_ids": []},
        )
        assert r.status_code == 201, r.text


class TestCurationByTheInstallationsAdministrator:
    """Curating the global model catalog, as the administrator of the
    installation (`require_platform_admin`): the system administrator where
    the edition under test has one, tenant t1's administrator where it has
    not (`_H.login_platform_admin`). Moved from the Enterprise half, which
    signed in as the system administrator and still runs these under that
    name (`TestCurationSystemAdmin`)."""

    def _new_entry(self):
        return {
            "provider_key": "openai",
            "model_name": "gpt-5",
            "display_name": "GPT-5",
            "mode": "chat",
            "recommended": True,
        }

    def test_create_ok(self, harness):
        hs = harness.login_platform_admin()
        r = harness.client.post(
            "/v1/admin/llm-catalog", headers=hs, json=self._new_entry()
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["id"].startswith("llmcat-")
        assert body["source"] == "custom"
        assert body["model_name"] == "gpt-5"
        assert body["recommended"] is True

    def test_create_duplicate_409(self, harness):
        hs = harness.login_platform_admin()
        dup = {
            "provider_key": "openai",
            "model_name": "gpt-4o",
            "display_name": "dup",
            "mode": "chat",
        }
        r = harness.client.post("/v1/admin/llm-catalog", headers=hs, json=dup)
        assert r.status_code == 409, r.text

    def test_create_bad_mode_400(self, harness):
        hs = harness.login_platform_admin()
        bad = {
            "provider_key": "openai",
            "model_name": "gpt-z",
            "display_name": "x",
            "mode": "bogus",
        }
        r = harness.client.post("/v1/admin/llm-catalog", headers=hs, json=bad)
        assert r.status_code == 400, r.text

    def test_update_ok(self, harness):
        hs = harness.login_platform_admin()
        r = harness.client.patch(
            "/v1/admin/llm-catalog/llmcat-openai-gpt-4o",
            headers=hs, json={"display_name": "Renamed", "deprecated": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["display_name"] == "Renamed"
        assert body["deprecated"] is True
        # untouched
        assert body["recommended"] is True

    def test_update_missing_404(self, harness):
        hs = harness.login_platform_admin()
        r = harness.client.patch(
            "/v1/admin/llm-catalog/llmcat-nope",
            headers=hs, json={"display_name": "x"},
        )
        assert r.status_code == 404

    def test_delete_ok(self, harness):
        hs = harness.login_platform_admin()
        r = harness.client.delete(
            "/v1/admin/llm-catalog/llmcat-openai-emb", headers=hs
        )
        assert r.status_code == 204, r.text
        # gone
        r2 = harness.client.get(
            "/v1/admin/llm-catalog/llmcat-openai-emb", headers=hs
        )
        assert r2.status_code == 404

    def test_delete_missing_404(self, harness):
        hs = harness.login_platform_admin()
        r = harness.client.delete(
            "/v1/admin/llm-catalog/llmcat-nope", headers=hs
        )
        assert r.status_code == 404
