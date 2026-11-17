# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The installation's own tenant, as its administrator sees it: what an update
of `/v1/admin/tenants/current` may change and who may send it, and reading
its theme. The Community Edition's part of the tenant admin API tests; runs
for both editions.

Tenant management (routes by tenant id, the system administrator, suspension,
deletion, the theme editor, the Knowledge Base routes) is the Enterprise
Edition's: `enterprise/tests/test_admin_tenants_scope_and_theme_http.py`,
from which these tests were moved unchanged and which imports the helpers
here.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/tenants"


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("tenant_settings_scope"):
        harness = build_admin_harness(
            [create_tenant_settings_router(BrutorLogger("test-tenants-scope"))],
            database_url=url,
        )
        yield harness
        harness.close()


def _tenant(h, tenant_id: str = "t1") -> dict:
    s = h.Factory()
    try:
        t = s.get(Tenant, tenant_id)
        return {
            "display_name": t.display_name,
            "suspended": t.suspended,
            "is_active": t.is_active,
            "primary": t.theme_primary_color,
            "company": t.theme_company_name,
            "logo": t.theme_logo_url,
            "data_residency": t.data_residency,
            "data_retention": t.data_retention,
            "approval": t.default_approval_window_seconds,
            "capture": t.assurance_capture_interval_hours,
            "gate": t.ai_system_gate_policy,
            "embedding": t.default_embedding_model_id,
            "max_llm_models": t.max_llm_models,
            "features": t.features,
        }
    finally:
        s.close()


def _set(h, tenant_id: str, **fields) -> None:
    def apply(s):
        t = s.get(Tenant, tenant_id)
        for k, v in fields.items():
            setattr(t, k, v)

    h.seed(apply)


class TestUpdate:

    @pytest.mark.parametrize(
        "field,value,state_key",
        [
            ("default_approval_window_seconds", 600, "approval"),
            ("assurance_capture_interval_hours", 24, "capture"),
            ("data_residency", {"allowed_regions": ["eu"], "block_global": True}, "data_residency"),
            ("data_retention", {"store_bodies": "full"}, "data_retention"),
        ],
    )
    def test_explicit_null_clears_what_a_value_set(self, h, field, value, state_key):
        r = h.client.patch(f"{BASE}/current", json={field: value}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _tenant(h, "t1")[state_key] is not None
        r = h.client.patch(f"{BASE}/current", json={"display_name": "T1"}, headers=h.headers("t1"))
        assert _tenant(h, "t1")[state_key] is not None, "an omitted key must not clear"
        r = h.client.patch(f"{BASE}/current", json={field: None}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _tenant(h, "t1")[state_key] is None

    def test_a_typo_in_the_gate_policy_is_refused_and_not_stored(self, h):
        before = _tenant(h, "t1")["gate"]
        r = h.client.patch(
            f"{BASE}/current", json={"ai_system_gate_policy": {"definitely_not_a_key": 1}}, headers=h.headers("t1")
        )
        assert r.status_code == 422
        assert _tenant(h, "t1")["gate"] == before

    def test_an_admin_with_no_write_permission_cannot_turn_off_residency(self, h):
        _set(h, "t1", data_residency={"allowed_regions": ["eu"], "block_global": True})
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        r = h.client.patch(f"{BASE}/current", json={"data_residency": None}, headers=limited)
        assert r.status_code == 403
        assert _tenant(h, "t1")["data_residency"] is not None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("data_residency", {"allowed_regions": ["eu"], "block_global": True}),
            ("data_retention", {"store_bodies": "full"}),
            ("display_name", "Renamed"),
        ],
    )
    def test_no_field_is_open_without_the_permission(self, h, field, value):
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        r = h.client.patch(f"{BASE}/current", json={field: value}, headers=limited)
        assert r.status_code == 403, r.text

    def test_the_permission_opens_the_route_for_a_limited_admin(self, h):
        limited = h.limited_headers("t1", [Permission.GATEWAY_CONFIG_UPDATE])
        r = h.client.patch(f"{BASE}/current", json={"data_residency": None}, headers=limited)
        assert r.status_code == 200, r.text


class TestTheme:

    def test_tenant_admin_reads_its_own(self, h):
        # At `/current/theme`; by id is the system admin's editor.
        r = h.client.get(f"{BASE}/current/theme", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["tenant_id"] == "t1"
        r = h.client.get(f"{BASE}/current/theme-variants", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["tenant_id"] == "t1"
