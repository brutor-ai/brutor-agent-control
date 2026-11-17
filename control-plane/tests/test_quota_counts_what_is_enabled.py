# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A quota that counts enabled rows is asked when a row is enabled.

A tenant's model and MCP server quotas count the rows that are switched on
(`TenantService.check_quota`), and were asked only when a row was created. A
row created switched off, or switched off for a moment, could be switched
on past any quota: the count was never taken on the update.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.mcp_server import MCPServer
from app.database.models.tenant import Tenant
from app.services.llm.model.llm_models_manager import LLModelsManager
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture()
def h():
    log = BrutorLogger("test-quota-enabled")
    manager = LLModelsManager(logger=log, encryption_service=None)
    mcp_router = create_mcp_server_router(MCPServerManager(log), log)
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=None), mcp_router],
        logger_name="test-quota-enabled",
    )

    def fn(s):
        s.add(LlmProvider(id="prov-openai", tenant_id="t1", key="openai", display_name="openai"))
        for n, enabled in enumerate((True, True, False)):
            s.add(MCPServer(id=f"srv-{n}", tenant_id="t1", name=f"srv-{n}", base_url="https://mcp.example", enabled=enabled))
        tenant = s.get(Tenant, "t1")
        tenant.max_llm_models = 2
        tenant.max_mcp_servers = 2

    harness.seed(fn)
    yield harness
    harness.close()


def _model(name, enabled):
    return {"name": name, "provider_id": "prov-openai", "model_name": "gpt-x", "mode": "chat", "enabled": enabled}


def test_enabling_a_model_past_the_quota_is_refused(h):
    ids = {}
    for name in ("a", "b"):
        r = h.client.post("/v1/admin/llms", json=_model(name, True), headers=h.headers())
        assert r.status_code == 201, r.text
        ids[name] = r.json()["model"]["id"]
    # Switch one off, add a third, and the quota is full again.
    assert h.client.patch(f"/v1/admin/llms/{ids['a']}", json={"enabled": False}, headers=h.headers()).status_code == 200
    r = h.client.post("/v1/admin/llms", json=_model("c", True), headers=h.headers())
    assert r.status_code == 201, r.text
    r = h.client.patch(f"/v1/admin/llms/{ids['a']}", json={"enabled": True}, headers=h.headers())
    assert r.status_code == 429, r.text
    with h.Factory() as s:
        assert s.get(LLModel, ids["a"]).enabled is False
        assert s.query(LLModel).filter(LLModel.tenant_id == "t1", LLModel.enabled.is_(True)).count() == 2
    # Another change of a row that is on, and of one that stays off, is not a new row.
    assert h.client.patch(f"/v1/admin/llms/{ids['b']}", json={"enabled": True, "description": "d"}, headers=h.headers()).status_code == 200
    assert h.client.patch(f"/v1/admin/llms/{ids['a']}", json={"description": "d"}, headers=h.headers()).status_code == 200
    # With room it is switched on.
    assert h.client.patch(f"/v1/admin/llms/{ids['b']}", json={"enabled": False}, headers=h.headers()).status_code == 200
    assert h.client.patch(f"/v1/admin/llms/{ids['a']}", json={"enabled": True}, headers=h.headers()).status_code == 200


def test_enabling_an_mcp_server_past_the_quota_is_refused(h):
    r = h.client.patch("/v1/admin/mcp-servers/srv-2", json={"enabled": True}, headers=h.headers())
    assert r.status_code == 429, r.text
    with h.Factory() as s:
        assert s.get(MCPServer, "srv-2").enabled is False
    assert h.client.patch("/v1/admin/mcp-servers/srv-0", json={"enabled": True}, headers=h.headers()).status_code == 200
    assert h.client.patch("/v1/admin/mcp-servers/srv-0", json={"enabled": False}, headers=h.headers()).status_code == 200
    assert h.client.patch("/v1/admin/mcp-servers/srv-2", json={"enabled": True}, headers=h.headers()).status_code == 200


def test_a_routing_groups_virtual_model_is_not_one_of_the_tenants_models(h):
    # The `rg-<id>` row a routing group is
    # addressed by is created enabled by the routing-group route, which asks
    # no quota, and was then counted as a model: a tenant at its quota that
    # created a routing group could add no model, and the usage shown was
    # one more than the models it has.
    from app.services.tenant.tenant_service import TenantService
    from app.constants.tenant import TenantResourceTypes
    from fastapi import HTTPException

    def fn(s):
        s.add(LLModel(id="rg-g1", tenant_id="t1", provider_id="prov-openai", name="[Routing Group] g1",
                      model_name="g1", mode="routing", enabled=True))
        s.add(LLModel(id="llm-one", tenant_id="t1", provider_id="prov-openai", name="one",
                      model_name="gpt-x", mode="chat", enabled=True))

    h.seed(fn)
    with h.Factory() as s:
        # One model of two: there is room for one more.
        TenantService.check_quota(s, "t1", TenantResourceTypes.LLM_MODELS)
        s.add(LLModel(id="llm-two", tenant_id="t1", provider_id="prov-openai", name="two",
                      model_name="gpt-x", mode="chat", enabled=True))
        s.flush()
        with pytest.raises(HTTPException) as refused:
            TenantService.check_quota(s, "t1", TenantResourceTypes.LLM_MODELS)
        assert refused.value.detail["current"] == 2
