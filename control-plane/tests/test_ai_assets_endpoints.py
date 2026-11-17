# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Registry HTTP endpoint tests

In-memory SQLite + FastAPI TestClient harness (mirrors
test_a2a_card_endpoints.py). Covers full CRUD, /sync, tenant-admin
pinning vs system-admin ?tenant_id, 404/409, and AI_ASSET_WRITE
permission enforcement.
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
from app.database.models.agent_identity import AgentIdentity
from app.database.models.agent_skill import AgentSkill
from app.database.models.ai_asset import AiAsset
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.agent_skill import resource_group_agent_skills
from app.database.models.agent_identity import ResourceGroupAgentMember
from app.database.models.skill_source import SkillSource
from app.database.models.tenant import Tenant
from tests.app_factory import harness_extras


JWT_SECRET = os.environ["JWT_SECRET"]


REQUIRED_TABLES = [
    LlmCatalog.__table__,       # LLModel.catalog_id FKs here
    LLModel.__table__,          # Tenant.default_embedding_model_id FKs here
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLMRoutingGroup.__table__,
    MCPServer.__table__,
    SkillSource.__table__,
    AgentSkill.__table__,
    AgentCard.__table__,
    ResourceGroup.__table__,    # asset_kind='ai_system' discovers resource_groups
    AgentIdentity.__table__,    # asset_kind='agent_identity' discovers agent_identities
    AiAsset.__table__,
    # a150_eu_ai_act_fields — the register's edge view (owning systems per component asset)
    # reads the group↔resource binding tables.
    resource_group_llm_models,
    resource_group_server_configs,
    resource_group_agent_skills,
    ResourceGroupAgentMember.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    IdPServer.__table__,        # TenantAdmin.idp_server_id FKs here
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
    logger = BrutorLogger("test-ai-assets")
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(logger):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_assets_router(logger), prefix="/v1")
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
            # Full-permission role.
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
            # Read-only role (AI_ASSET_READ but NOT AI_ASSET_WRITE).
            ro_role = TenantRole(
                id=f"role-{tid}-ro", tenant_id=tid,
                name="Asset Reader", description="read-only",
                is_system=False,
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

    def login_system(self) -> dict:
        r = self.client.post(
            "/v1/admin/users/system/login",
            json={"username": "sysadmin", "password": "Secret123!"},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def seed_sources(self, tenant_id="t1"):
        s = self.Factory()
        try:
            s.add(LlmProvider(id="prov-1", tenant_id=tenant_id, key="openai",
                              display_name="OpenAI"))
            s.flush()
            s.add(LLModel(id="model-1", tenant_id=tenant_id,
                          provider_id="prov-1", name="GPT-4o",
                          model_name="gpt-4o"))
            s.add(LLMRoutingGroup(id="rg-1", tenant_id=tenant_id, name="Pool"))
            s.add(MCPServer(id="mcp-1", tenant_id=tenant_id, name="Files",
                            base_url="https://mcp.example"))
            s.add(AgentSkill(id="skill-1", tenant_id=tenant_id,
                             name="summarise", description="d",
                             skill_md_content="# x", skill_md_hash="h"))
            s.add(AgentCard(id="card-1", tenant_id=tenant_id, name="Billing",
                            card_json={"name": "b", "capabilities": []}))
            s.commit()
        finally:
            s.close()


# =============================================================================
# CRUD happy path
# =============================================================================


class TestCrud:
    def test_create_then_list_and_get(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "llm_model", "ref_id": "model-x",
                  "display_name": "GPT", "owner": "alice",
                  "gpai_classification": "gpai", "operated_by": "third_party",
                  "tags": ["pii"]},
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["id"].startswith("aiasset-")
        assert created["source"] == "manual"
        assert created["lifecycle_stage"] == "active"
        # a150_eu_ai_act_fields: risk_tier is a SYSTEM property — component rows derive it,
        # so this llm_model row has no tier of its own.
        assert created["risk_tier"] is None
        assert created["gpai_classification"] == "gpai"
        assert created["operated_by"] == "third_party"
        assert created["tags"] == ["pii"]

        lst = harness.client.get("/v1/admin/assets", headers=h)
        assert lst.status_code == 200
        assert lst.json()["total"] == 1

        got = harness.client.get(f"/v1/admin/assets/{created['id']}", headers=h)
        assert got.status_code == 200
        assert got.json()["owner"] == "alice"

    def test_update(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "skill", "ref_id": "s1"},
        ).json()
        r = harness.client.patch(
            f"/v1/admin/assets/{created['id']}", headers=h,
            json={"owner": "bob", "lifecycle_stage": "deprecated",
                  "status": "flagged"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["owner"] == "bob"
        assert body["lifecycle_stage"] == "deprecated"
        assert body["status"] == "flagged"

    def test_delete(self, harness):
        h = harness.login("t1")
        created = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "skill", "ref_id": "s1"},
        ).json()
        r = harness.client.delete(f"/v1/admin/assets/{created['id']}", headers=h)
        assert r.status_code == 204
        r = harness.client.get(f"/v1/admin/assets/{created['id']}", headers=h)
        assert r.status_code == 404

    def test_create_duplicate_409(self, harness):
        h = harness.login("t1")
        harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "skill", "ref_id": "dup"},
        )
        r = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "skill", "ref_id": "dup"},
        )
        assert r.status_code == 409

    def test_get_missing_404(self, harness):
        h = harness.login("t1")
        r = harness.client.get("/v1/admin/assets/aiasset-nope", headers=h)
        assert r.status_code == 404

    def test_create_bad_kind_400(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "bogus", "ref_id": "x"},
        )
        assert r.status_code == 400


# =============================================================================
# Sync
# =============================================================================


class TestSync:
    def test_sync_discovers_all_kinds(self, harness):
        harness.seed_sources("t1")
        h = harness.login("t1")
        r = harness.client.post("/v1/admin/assets/sync", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == 6
        assert body["total"] == 6
        lst = harness.client.get("/v1/admin/assets", headers=h).json()
        kinds = {a["asset_kind"] for a in lst["assets"]}
        assert kinds == {"llm_model", "llm_provider", "routing_group",
                         "mcp_server", "skill", "agent_card"}

    def test_list_filter_by_kind(self, harness):
        harness.seed_sources("t1")
        h = harness.login("t1")
        harness.client.post("/v1/admin/assets/sync", headers=h)
        r = harness.client.get(
            "/v1/admin/assets?asset_kind=mcp_server", headers=h
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["assets"][0]["asset_kind"] == "mcp_server"


# =============================================================================
# Tenant isolation + admin pinning
# =============================================================================


class TestTenantScope:
    def test_t1_admin_cannot_get_t2_asset(self, harness):
        h2 = harness.login("t2")
        created = harness.client.post(
            "/v1/admin/assets", headers=h2,
            json={"asset_kind": "skill", "ref_id": "t2-only"},
        ).json()
        h1 = harness.login("t1")
        r = harness.client.get(f"/v1/admin/assets/{created['id']}", headers=h1)
        assert r.status_code == 404


# =============================================================================
# Permission enforcement
# =============================================================================


class TestPermissions:
    def test_reader_can_list(self, harness):
        h = harness.login_reader("t1")
        r = harness.client.get("/v1/admin/assets", headers=h)
        assert r.status_code == 200

    def test_reader_cannot_create(self, harness):
        h = harness.login_reader("t1")
        r = harness.client.post(
            "/v1/admin/assets", headers=h,
            json={"asset_kind": "skill", "ref_id": "nope"},
        )
        assert r.status_code == 403

    def test_reader_cannot_sync(self, harness):
        h = harness.login_reader("t1")
        r = harness.client.post("/v1/admin/assets/sync", headers=h)
        assert r.status_code == 403
