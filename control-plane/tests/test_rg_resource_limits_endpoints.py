# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Per-item RG↔resource LIMITS endpoint round-trips.

The per-(group, resource) limit overrides live on the association-table
``limits`` JSON columns:

  * resource_group_llm_models.limits    (LLM)
  * resource_group_server_configs.limits (MCP)
  * resource_group_agent_skills.limits   (Skill)

driven by the admin UI through:

  * PATCH/GET ``/admin/resource-groups/{gid}/llm-models/{mid}/limits``
  * PATCH/GET ``/admin/resource-groups/{gid}/mcp-servers/{sid}/limits``
  * PATCH/GET ``/admin/resource-groups/{gid}/agent-skills/{sid}/limits``

Only the validators were previously covered (test_resource_group_limits_
validation.py / test_mcp_rate_limit_validation.py); this exercises the full
PATCH→persist→GET round-trip through the real routes and asserts every limit
key survives on the ``limits`` field of the response (the recurring "Pydantic
strips undeclared fields" gotcha would drop a key if the response model ever
stopped carrying the raw blob).

Run: pytest tests/test_rg_resource_limits_endpoints.py -q
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
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import TenantRole, TenantRolePermission
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]


# =============================================================================
# Limit key sets — a broad representative slice of each policy shape.
# =============================================================================

LLM_LIMITS = {
    "throughput": {
        "max_requests_per_minute": 60,
        "max_input_tokens_per_minute": 100000,
        "max_output_tokens_per_minute": 20000,
        "max_usd_per_minute": 0.5,
        "cooldown_seconds": 30,
        "request_burst": 10,
    },
    "budget": {
        "daily_limit_usd": 100.0,
        "monthly_limit_usd": 3000.0,
        "daily_warning_percent": 80,
        "monthly_warning_percent": 90,
        "hard_stop": True,
    },
    "tokens": {
        "daily_limit": 1000000,
        "monthly_limit": 30000000,
        "daily_warning_percent": 85,
        "monthly_warning_percent": 90,
    },
    "requests": {
        "daily_limit": 10000,
        "monthly_limit": 300000,
        "daily_warning_percent": 80,
        "monthly_warning_percent": 85,
    },
    "concurrency": {"max_concurrent": 8},
}

MCP_LIMITS = {
    "execution": {"timeout_seconds": 30, "max_concurrency_per_user": 4},
    "data": {"max_payload_size_mb": 5},
    "frequency": {
        "max_calls_per_minute": 10,
        "max_calls_per_hour": 100,
        "max_calls_per_day": 1000,
        "burst_limit": 20,
        "cooldown_seconds": 30,
        "daily_warning_percent": 80,
    },
}

SKILL_LIMITS = {
    "execution": {
        "daily_limit": 1000,
        "monthly_limit": 30000,
        "daily_warning_percent": 80,
        "monthly_warning_percent": 90,
    },
}


def _assert_nested_round_trip(expected: dict, stored: dict):
    """Every section + every leaf key in `expected` survives in `stored`."""
    for section, keys in expected.items():
        assert section in stored, f"section '{section}' dropped"
        for key, val in keys.items():
            assert stored[section][key] == val, f"{section}.{key} did not round-trip"


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
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, SessionFactory=SessionFactory)
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, SessionFactory):
        self.client = client
        self.SessionFactory = SessionFactory

    def login(self):
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "adm1", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def seed_group_with_bindings(self, rg_id="rg-1"):
        """RG + provider + one LLM model, MCP server and agent skill, each
        bound to the RG (a direct association row is required for the PATCH
        limits endpoints)."""
        s = self.SessionFactory()
        try:
            s.add(ResourceGroup(
                id=rg_id, tenant_id="t1", name="Engineering",
                display_name="Engineering", hierarchy_level=0, is_active=True,
            ))
            s.add(LlmProvider(id="prov-x", tenant_id="t1", key="openai", display_name="OpenAI"))
            s.add(LLModel(
                id="mdl-1", tenant_id="t1", name="GPT", provider_id="prov-x",
                model_name="gpt", mode="chat",
            ))
            s.add(ServerConfig(id="srv-1", tenant_id="t1", name="hubspot"))
            s.add(AgentSkill(
                id="skl-1", tenant_id="t1", name="reporter",
                description="report skill", skill_md_content="# skill",
                skill_md_hash="deadbeef",
            ))
            s.flush()
            s.execute(resource_group_llm_models.insert().values(
                id=str(uuid.uuid4()), resource_group_id=rg_id, llm_model_id="mdl-1",
            ))
            s.execute(resource_group_server_configs.insert().values(
                id=str(uuid.uuid4()), resource_group_id=rg_id, server_config_id="srv-1",
            ))
            s.execute(resource_group_agent_skills.insert().values(
                id=str(uuid.uuid4()), resource_group_id=rg_id, agent_skill_id="skl-1",
            ))
            s.commit()
            return rg_id
        finally:
            s.close()


# =============================================================================
# LLM per-model limits
# =============================================================================

def test_llm_model_limits_round_trip(harness):
    headers = harness.login()
    rg = harness.seed_group_with_bindings()

    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/llm-models/mdl-1/limits",
        headers=headers, json={"limits": LLM_LIMITS},
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(LLM_LIMITS, r.json()["limits"])

    r = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/llm-models/mdl-1/limits", headers=headers,
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(LLM_LIMITS, r.json()["limits"])


# =============================================================================
# MCP per-server limits
# =============================================================================

def test_mcp_server_limits_round_trip(harness):
    headers = harness.login()
    rg = harness.seed_group_with_bindings()

    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/mcp-servers/srv-1/limits",
        headers=headers, json={"limits": MCP_LIMITS},
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(MCP_LIMITS, r.json()["limits"])

    r = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/mcp-servers/srv-1/limits", headers=headers,
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(MCP_LIMITS, r.json()["limits"])


# =============================================================================
# Skill per-item limits
# =============================================================================

def test_agent_skill_limits_round_trip(harness):
    headers = harness.login()
    rg = harness.seed_group_with_bindings()

    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/agent-skills/skl-1/limits",
        headers=headers, json={"limits": SKILL_LIMITS},
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(SKILL_LIMITS, r.json()["limits"])

    r = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/agent-skills/skl-1/limits", headers=headers,
    )
    assert r.status_code == 200, r.text
    _assert_nested_round_trip(SKILL_LIMITS, r.json()["limits"])


# =============================================================================
# CP-17 — capacity helpers resolve names in one round trip
# =============================================================================

def test_per_model_capacity_resolves_names_without_a_query_per_binding(harness):
    """The helper used to issue one SELECT per bound model just to turn an
    id into a display name; a group with 40 bound models was 41 queries."""
    from sqlalchemy import event

    from app.api.v1.endpoints.admin.resource_groups import (
        _compute_per_model_capacity_info,
    )

    rg = harness.seed_group_with_bindings()
    s = harness.SessionFactory()
    try:
        group = s.query(ResourceGroup).filter(ResourceGroup.id == rg).one()
        group.llm_global_limits = {"requests_per_minute": 1000}
        # Six more bound models, each carrying a per-model limit so they
        # all land in `other_models`.
        for n in range(2, 8):
            s.add(LLModel(
                id=f"mdl-{n}", tenant_id="t1", name=f"Model {n}",
                provider_id="prov-x", model_name=f"m{n}", mode="chat",
            ))
            s.execute(resource_group_llm_models.insert().values(
                id=str(uuid.uuid4()), resource_group_id=rg,
                llm_model_id=f"mdl-{n}", limits={"requests_per_minute": 10},
            ))
        s.commit()

        statements = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = s.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            info = _compute_per_model_capacity_info(group, "mdl-1", s)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert info is not None
        assert len(info.other_models) == 6
        # Names still resolve — the batch lookup must not regress to ids.
        assert {m.llm_model_name for m in info.other_models} == {
            f"Model {n}" for n in range(2, 8)
        }
        # One name lookup for the whole set, not one per binding. Counted
        # against `llm_model` specifically so unrelated hierarchy lazy-loads
        # in get_effective_llm_limits() do not blunt the bound.
        model_queries = [q for q in statements if "FROM llm_model" in q]
        assert len(model_queries) == 1, "\n".join(model_queries)
    finally:
        s.close()


def test_capacity_falls_back_to_the_id_when_the_row_is_gone(harness):
    """A binding whose model row was deleted still has to render."""
    from app.api.v1.endpoints.admin.resource_groups import _names_by_id

    s = harness.SessionFactory()
    try:
        assert _names_by_id(s, LLModel, "t1", []) == {}
        assert _names_by_id(s, LLModel, "t1", ["nope"]) == {}
    finally:
        s.close()
