# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Prompt-cache policies, gateway health-check config and LLM providers over HTTP:
tenant isolation, permission denial, validation, and what lands in the database.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.gateway_config import create_gateway_config_admin_router
from app.api.v1.endpoints.admin.llm_providers import create_llm_providers_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.prompt_cache_policy import PromptCachePolicy
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

PC = "/v1/admin/prompt-cache"
GW = "/v1/admin/gateway-config"
PR = "/v1/admin/providers"
_n = {"i": 0}


def _next() -> str:
    _n["i"] += 1
    return f"{_n['i']:04d}"


def harness_with(extra_routers=()):
    """The file's harness: the gateway-config and providers routers, and the
    routers an edition adds (the Enterprise half mounts the prompt-cache router)."""
    log = BrutorLogger("test-pc-gw-pr")
    for url in throwaway_pg_database("pc_gw_pr"):
        harness = build_admin_harness(
            [*extra_routers, create_gateway_config_admin_router(log), create_llm_providers_router(log)],
            database_url=url,
        )

        def seed(s):
            for tid in ("t1", "t2"):
                s.add(LlmProvider(id=f"prov-{tid}", tenant_id=tid, key="openai", display_name="OpenAI"))
                s.flush()
                s.add(LLModel(id=f"m-{tid}", tenant_id=tid, provider_id=f"prov-{tid}", name="m", model_name="m", enabled=True))

        harness.seed(seed)
        yield harness
        harness.close()


@pytest.fixture(scope="module")
def h():
    yield from harness_with()


def _policy(h, tenant="t1", **over) -> dict:
    r = h.client.post(f"{PC}/policies", json={"name": f"pc-{_next()}", **over}, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _policy_row(h, pid):
    s = h.Factory()
    try:
        p = s.get(PromptCachePolicy, pid)
        return None if p is None else {"name": p.name, "enabled": p.enabled, "ttl": p.ttl_seconds, "tenant": p.tenant_id}
    finally:
        s.close()


class TestGatewayConfig:
    def test_first_read_creates_defaults_for_this_tenant_only(self, h):
        r = h.client.get(f"{GW}/health-config", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "mcp_settings" in r.text

    def test_update_is_partial_and_tenant_scoped(self, h):
        h.client.post(f"{GW}/health-config/reset", headers=h.headers("t2"))
        r = h.client.patch(
            f"{GW}/health-config",
            json={"mcp_settings": {"enabled": False, "interval_seconds": 120}},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        mine = h.client.get(f"{GW}/health-config", headers=h.headers("t1")).json()
        theirs = h.client.get(f"{GW}/health-config", headers=h.headers("t2")).json()
        settings = lambda d: d.get("config", d)["mcp_settings"]
        assert settings(mine)["interval_seconds"] == 120 and settings(mine)["enabled"] is False
        assert settings(theirs)["interval_seconds"] != 120

    def test_every_section_can_be_updated(self, h):
        r = h.client.patch(
            f"{GW}/health-config",
            json={
                "dashboard_settings": {"enabled": True, "interval_seconds": 60},
                "mcp_settings": {"enabled": True, "interval_seconds": 120, "pause_when_inactive": False},
                "ai_settings": {"enabled": True, "interval_seconds": 300, "failure_threshold": 2, "pause_when_inactive": False},
            },
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        # The dashboard's settings are saved too (they were accepted and dropped).
        assert r.json()["dashboard_settings"]["interval_seconds"] == 60
        r = h.client.patch(f"{GW}/health-config", json={"dashboard_settings": {"interval_seconds": 45}}, headers=h.headers("t1"))
        assert r.json()["dashboard_settings"]["interval_seconds"] == 45

    def test_out_of_range_is_refused(self, h):
        r = h.client.patch(f"{GW}/health-config", json={"mcp_settings": {"interval_seconds": 1}}, headers=h.headers("t1"))
        assert r.status_code == 422

    def test_reset_restores_the_defaults(self, h):
        h.client.patch(f"{GW}/health-config", json={"mcp_settings": {"interval_seconds": 120}}, headers=h.headers("t1"))
        r = h.client.post(f"{GW}/health-config/reset", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        got = h.client.get(f"{GW}/health-config", headers=h.headers("t1")).json()
        assert got.get("config", got)["mcp_settings"]["interval_seconds"] != 120

    def test_permissions(self, h):
        ro = h.limited_headers("t1", [Permission.GATEWAY_CONFIG_READ])
        none = h.limited_headers("t1", [Permission.GUARDRAIL_READ])
        assert h.client.get(f"{GW}/health-config", headers=none).status_code == 403
        assert h.client.get(f"{GW}/health-config", headers=ro).status_code == 200
        assert h.client.patch(f"{GW}/health-config", json={}, headers=ro).status_code == 403
        assert h.client.post(f"{GW}/health-config/reset", headers=ro).status_code == 403


def _provider(h, tenant="t1", **over) -> dict:
    n = _next()
    r = h.client.post(PR, json={"key": f"custom-{n}", "display_name": f"Custom {n}", **over}, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _prov_row(h, pid):
    s = h.Factory()
    try:
        row = s.get(LlmProvider, pid)
        return None if row is None else {"key": row.key, "display_name": row.display_name, "enabled": row.enabled, "tenant": row.tenant_id}
    finally:
        s.close()


class TestProviders:
    @pytest.mark.parametrize(
        "method,suffix,kw",
        [
            ("get", "", {}),
            ("patch", "", {"json": {"display_name": "stolen"}}),
            ("delete", "", {}),
            ("post", "/rename?new_key=stolen", {}),
        ],
    )
    def test_another_tenants_provider_looks_absent(self, h, method, suffix, kw):
        p = _provider(h, "t2")
        r = getattr(h.client, method)(f"{PR}/{p['id']}{suffix}", headers=h.headers("t1"), **kw)
        assert r.status_code == 404, r.text
        assert _prov_row(h, p["id"])["display_name"] == p["display_name"]

    def test_listing_is_tenant_scoped(self, h):
        mine, theirs = _provider(h, "t1"), _provider(h, "t2")
        r = h.client.get(PR, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert mine["id"] in r.text and theirs["id"] not in r.text

    def test_a_duplicate_key_is_a_conflict(self, h):
        p = _provider(h)
        r = h.client.post(PR, json={"key": p["key"], "display_name": "x"}, headers=h.headers("t1"))
        assert r.status_code == 409

    def test_update_clears_and_sets(self, h):
        p = _provider(h, region="eu")
        r = h.client.patch(f"{PR}/{p['id']}", json={"display_name": "New", "region": None}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "New" and r.json().get("region") is None

    def test_rename_rules(self, h):
        p = _provider(h)
        assert h.client.post(f"{PR}/{p['id']}/rename?new_key=BAD%20KEY", headers=h.headers("t1")).status_code == 400
        new_key = f"renamed-{_next()}"
        r = h.client.post(f"{PR}/{p['id']}/rename?new_key={new_key}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _prov_row(h, p["id"])["key"] == new_key

    def test_a_provider_in_use_cannot_be_renamed_or_deleted(self, h):
        assert h.client.post(f"{PR}/prov-t1/rename?new_key=other-key", headers=h.headers("t1")).status_code == 409
        assert h.client.delete(f"{PR}/prov-t1", headers=h.headers("t1")).status_code == 409
        assert _prov_row(h, "prov-t1")["key"] == "openai"

    def test_delete_an_unused_provider(self, h):
        p = _provider(h)
        assert h.client.delete(f"{PR}/{p['id']}", headers=h.headers("t1")).status_code in (200, 204)
        assert _prov_row(h, p["id"]) is None

    def test_a_system_provider_cannot_be_deleted(self, h):
        p = _provider(h)

        def mark(s):
            s.get(LlmProvider, p["id"]).is_system = True

        h.seed(mark)
        assert h.client.delete(f"{PR}/{p['id']}", headers=h.headers("t1")).status_code == 409
        assert _prov_row(h, p["id"]) is not None

    def test_permissions(self, h):
        p = _provider(h)
        ro = h.limited_headers("t1", [Permission.AI_MODEL_READ])
        assert h.client.get(f"{PR}/{p['id']}", headers=ro).status_code == 200
        assert h.client.patch(f"{PR}/{p['id']}", json={"display_name": "x"}, headers=ro).status_code == 403
        assert h.client.delete(f"{PR}/{p['id']}", headers=ro).status_code == 403
        assert h.client.post(PR, json={"key": "nope-key", "display_name": "x"}, headers=ro).status_code == 403
