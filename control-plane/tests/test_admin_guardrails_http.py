# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the guardrails admin API (/v1/admin/guardrails).

Provider
round-trip tests (Presidio, external moderation) live elsewhere; this file
defends the configuration contract:

  * **Config CRUD with group binding** — configs are the unit resource
    groups attach to, and the most-restrictive-wins semantics documented in
    the Admin UI start from these rows being right.
  * **The legacy singleton /config surface still answers** — older admin
    clients read it.
  * **Tenant isolation** on every mutation.
"""

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.core.logging_system import BrutorLogger
from app.services.guardrails.guardrails_service import GuardrailsService


@pytest.fixture()
def h():
    log = BrutorLogger("test-guardrails")
    harness = build_admin_harness(
        [create_guardrails_router(GuardrailsService(logger=log), log)],
        logger_name="test-guardrails",
    )
    yield harness
    harness.close()


def _create(h, tenant="t1", name="baseline", **over):
    body = {"name": name, "description": "baseline checks", "enabled": True}
    body.update(over)
    r = h.client.post(
        "/v1/admin/guardrails/configs", json=body, headers=h.headers(tenant)
    )
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("config", payload)


class TestConfigCrud:
    def test_create_returns_the_config(self, h):
        cfg = _create(h)
        assert cfg["name"] == "baseline"
        assert cfg["enabled"] is True

    def test_get_by_id_round_trips(self, h):
        cid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/guardrails/configs/{cid}", headers=h.headers()
        )
        assert r.status_code == 200, r.text
        assert r.json().get("config", r.json())["id"] == cid

    def test_listing_contains_it(self, h):
        cid = _create(h)["id"]
        r = h.client.get("/v1/admin/guardrails/configs", headers=h.headers())
        assert r.status_code == 200, r.text
        payload = r.json()
        configs = payload.get("configs", payload if isinstance(payload, list) else [])
        assert any(c["id"] == cid for c in configs)

    def test_patch_updates_priority_and_flag(self, h):
        cid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/guardrails/configs/{cid}",
            json={"priority": 10, "enabled": False},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        cfg = r.json().get("config", r.json())
        assert cfg["priority"] == 10
        assert cfg["enabled"] is False

    def test_delete_removes_it(self, h):
        cid = _create(h)["id"]
        r = h.client.delete(
            f"/v1/admin/guardrails/configs/{cid}", headers=h.headers()
        )
        assert r.status_code in (200, 204), r.text
        assert (
            h.client.get(
                f"/v1/admin/guardrails/configs/{cid}", headers=h.headers()
            ).status_code
            == 404
        )

    def test_unknown_config_is_404(self, h):
        r = h.client.get(
            "/v1/admin/guardrails/configs/nope", headers=h.headers()
        )
        assert r.status_code == 404

    def test_missing_name_is_422(self, h):
        r = h.client.post(
            "/v1/admin/guardrails/configs",
            json={"description": "nameless"},
            headers=h.headers(),
        )
        assert r.status_code == 422


class TestGroupBinding:
    def test_groups_can_be_set_on_a_config(self, h):
        cid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/guardrails/configs/{cid}/groups",
            json={"group_ids": ["group-default-t1"]},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_binding_to_a_foreign_group_is_rejected(self, h):
        cid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/guardrails/configs/{cid}/groups",
            json={"group_ids": ["group-default-t2"]},
            headers=h.headers("t1"),
        )
        assert r.status_code == 404, r.text
        assert "not found" in r.json()["detail"].lower()


class TestLegacySingleton:
    def test_the_config_surface_answers(self, h):
        r = h.client.get("/v1/admin/guardrails/config", headers=h.headers())
        assert r.status_code == 200, r.text

    def test_the_config_surface_accepts_a_patch(self, h):
        r = h.client.patch(
            "/v1/admin/guardrails/config",
            json={"enabled": True},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestProvidersAndDefaults:
    def test_providers_listing_answers(self, h):
        r = h.client.get("/v1/admin/guardrails/providers", headers=h.headers())
        assert r.status_code == 200, r.text

    def test_recommended_mode_defaults_apply(self, h):
        cid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/guardrails/{cid}/apply-recommended-mode-defaults",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestIsolation:
    def test_other_tenant_cannot_read_or_delete(self, h):
        cid = _create(h, tenant="t1")["id"]
        assert (
            h.client.get(
                f"/v1/admin/guardrails/configs/{cid}", headers=h.headers("t2")
            ).status_code
            == 404
        )
        assert (
            h.client.delete(
                f"/v1/admin/guardrails/configs/{cid}", headers=h.headers("t2")
            ).status_code
            == 404
        )
