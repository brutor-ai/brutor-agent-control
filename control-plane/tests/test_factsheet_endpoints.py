# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Factsheet HTTP endpoint tests

In-memory SQLite + FastAPI TestClient harness (mirrors
test_ai_assets_endpoints.py). Covers the live factsheet (JSON + CSV
export), snapshot capture / list / get, auth + tenant scoping, 404s and
AI_ASSET_WRITE permission enforcement on capture.
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
from app.api.v1.endpoints.admin.ai_assets import create_ai_assets_router
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
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.ai_asset import AiAsset
from app.database.models.ai_asset_factsheet import AiAssetFactsheet
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.proxy_log import ProxyLog
from app.database.models.usage_log import UsageLog
from app.database.models.end_user import EndUser
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.skill_source import SkillSource
from app.database.models.tenant import Tenant
from app.database.models.virtual_server import VirtualServer


JWT_SECRET = os.environ["JWT_SECRET"]


REQUIRED_TABLES = [
    LlmCatalog.__table__,   # LLModel.catalog_id FKs here
    LLModel.__table__,
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLMRoutingGroup.__table__,
    MCPServer.__table__,
    VirtualServer.__table__,
    OAuthAppClient.__table__,
    ServerConfig.__table__,
    SkillSource.__table__,
    AgentSkill.__table__,
    AgentCard.__table__,
    AiAsset.__table__,
    AiAssetFactsheet.__table__,
    ProxyLog.__table__,
    UsageLog.__table__,
    EndUser.__table__,   # usage_logs.user_id FK parent
    ResourceGroup.__table__,
    resource_group_llm_models,
    resource_group_server_configs,
    resource_group_agent_skills,
    resource_group_agent_cards,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    IdPServer.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
]


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

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
    logger = BrutorLogger("test-factsheet-endpoints")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_assets_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid, name in [("t1", "Tenant One"), ("t2", "Tenant Two")]:
            s.add(Tenant(
                id=tid, name=tid, display_name=name,
                organization_name=name,
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
            ))
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin", tenant_id=tid,
                name="Tenant Admin", description="full-perm", is_system=True,
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
            ro_role = TenantRole(
                id=f"role-{tid}-ro", tenant_id=tid,
                name="Asset Reader", description="read-only", is_system=False,
            )
            s.add(ro_role)
            s.flush()
            s.add(TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=ro_role.id, permission=Permission.AI_ASSET_READ.value,
            ))
            auth.create_tenant_admin(
                username=f"reader_{tid}", email=f"reader@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=ro_role.id,
            )
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

    def seed_llm_asset(self, tenant_id="t1", model_id="model-1") -> str:
        """Seed a provider + model + an ai_assets row + a couple of
        proxy_logs rows. Returns the asset id."""
        s = self.Factory()
        try:
            s.add(LlmProvider(id=f"prov-{model_id}", tenant_id=tenant_id,
                              key=f"k-{model_id}", display_name="OpenAI"))
            s.flush()
            s.add(LLModel(id=model_id, tenant_id=tenant_id,
                          provider_id=f"prov-{model_id}", name="GPT",
                          model_name="gpt-4o"))
            s.add(ResourceGroup(id=f"grp-{model_id}", tenant_id=tenant_id,
                                name="Engineering"))
            s.flush()
            s.execute(resource_group_llm_models.insert().values(
                id=str(uuid.uuid4()),
                resource_group_id=f"grp-{model_id}",
                llm_model_id=model_id,
            ))
            for prompt, completion, success in [(100, 50, True), (20, None, False)]:
                s.add(ProxyLog(
                    id=f"plog-{uuid.uuid4().hex}", tenant_id=tenant_id,
                    proxy_type="llm", server_id=model_id,
                    end_user_session_id=f"sess-{uuid.uuid4().hex[:8]}",
                    http_method="POST", endpoint="/v1/proxy/llm",
                    success=success, llm_prompt_tokens=prompt,
                    llm_completion_tokens=completion, llm_estimated_cost_usd=0.05,
                    duration_ms=150.0,
                ))
            asset = AiAsset(
                id=f"aiasset-{uuid.uuid4().hex}", tenant_id=tenant_id,
                asset_kind="llm_model", ref_id=model_id, display_name="GPT",
            )
            s.add(asset)
            s.commit()
            return asset.id
        finally:
            s.close()


# =============================================================================
# Live factsheet — JSON
# =============================================================================


class TestLiveFactsheet:
    def test_get_factsheet_json(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet", headers=h
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["asset"]["id"] == asset_id
        assert body["usage"]["requests"] == 2
        # Derived total = 100+50 + 20+0 = 170.
        assert body["usage"]["total_tokens"] == 170
        assert body["usage"]["success_count"] == 1
        assert body["usage"]["error_count"] == 1
        assert body["access"]["resource_groups"][0]["name"] == "Engineering"
        assert body["meta"]["period_hours"] == 720

    def test_get_factsheet_all_time(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet?period_hours=0", headers=h
        )
        assert r.status_code == 200, r.text
        assert r.json()["meta"]["period_hours"] == 0

    def test_get_factsheet_missing_404(self, harness):
        h = harness.login("t1")
        r = harness.client.get(
            "/v1/admin/assets/aiasset-nope/factsheet", headers=h
        )
        assert r.status_code == 404

    def test_factsheet_tenant_scoped(self, harness):
        asset_id = harness.seed_llm_asset(tenant_id="t2", model_id="m-t2")
        h1 = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet", headers=h1
        )
        assert r.status_code == 404

    def test_reader_can_get_factsheet(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login_reader("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet", headers=h
        )
        assert r.status_code == 200


# =============================================================================
# Live factsheet — CSV export
# =============================================================================


class TestCsvExport:
    def test_csv_export(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet?format=csv", headers=h
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        text = r.text
        # Meta comment block + header row + one data row.
        assert "# brutor-export" in text
        assert "asset_id" in text
        assert asset_id in text
        assert "total_tokens" in text


# =============================================================================
# Snapshots
# =============================================================================


class TestSnapshots:
    def test_capture_then_list_then_get(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login("t1")

        cap = harness.client.post(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshot", headers=h
        )
        assert cap.status_code == 201, cap.text
        snap = cap.json()
        assert snap["id"].startswith("aiasset-fs-")
        assert snap["asset_id"] == asset_id
        assert snap["period_hours"] == 720
        assert snap["trigger"] == "manual"
        assert snap["snapshot_json"]["usage"]["total_tokens"] == 170

        lst = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshots", headers=h
        )
        assert lst.status_code == 200
        assert lst.json()["total"] == 1
        assert lst.json()["snapshots"][0]["id"] == snap["id"]

        got = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshots/{snap['id']}",
            headers=h,
        )
        assert got.status_code == 200
        assert got.json()["id"] == snap["id"]

    def test_snapshot_missing_404(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login("t1")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshots/aiasset-fs-nope",
            headers=h,
        )
        assert r.status_code == 404

    def test_snapshot_cross_tenant_404(self, harness):
        asset_id = harness.seed_llm_asset()
        h1 = harness.login("t1")
        snap_id = harness.client.post(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshot", headers=h1
        ).json()["id"]
        # t2 admin cannot read t1's snapshot (asset is t1-only → 404 on
        # the asset-scoped list, and the snapshot fetch is tenant-scoped).
        h2 = harness.login("t2")
        r = harness.client.get(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshots/{snap_id}",
            headers=h2,
        )
        assert r.status_code == 404

    def test_reader_cannot_capture(self, harness):
        asset_id = harness.seed_llm_asset()
        h = harness.login_reader("t1")
        r = harness.client.post(
            f"/v1/admin/assets/{asset_id}/factsheet/snapshot", headers=h
        )
        assert r.status_code == 403

    def test_capture_missing_asset_404(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/assets/aiasset-nope/factsheet/snapshot", headers=h
        )
        assert r.status_code == 404
