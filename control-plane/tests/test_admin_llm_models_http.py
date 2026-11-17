# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the LLM models admin API (/v1/admin/llms).

Health
checks and connection tests reach the network and are exercised elsewhere;
this file defends the configuration contract:

  * **A model needs a provider** — the endpoint accepts provider_id or the
    legacy slug, and rejects a model with neither.
  * **Group assignment is the access model.** Assign/remove round-trips,
    and the by-group view reflects them.
  * **Copy produces an independent row.**
  * **Tenant isolation** on read, update, delete.
"""

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_provider import LlmProvider
from app.services.llm.model.llm_models_manager import LLModelsManager


@pytest.fixture()
def h():
    log = BrutorLogger("test-llm-models")
    manager = LLModelsManager(logger=log, encryption_service=None)
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=None)],
        logger_name="test-llm-models",
    )

    def fn(s):
        for tid in ("t1", "t2"):
            s.add(
                LlmProvider(
                    id=f"prov-{tid}",
                    tenant_id=tid,
                    key="openai",
                    display_name="OpenAI",
                )
            )

    harness.seed(fn)
    yield harness
    harness.close()


def _mk(name="gpt-test", **over):
    body = {
        "name": name,
        "provider_id": "prov-t1",
        "model_name": "gpt-4o-mini",
        "mode": "chat",
        "enabled": True,
    }
    body.update(over)
    return body


def _create(h, tenant="t1", name="gpt-test", **over):
    if tenant != "t1" and "provider_id" not in over:
        over["provider_id"] = f"prov-{tenant}"
    r = h.client.post(
        "/v1/admin/llms", json=_mk(name, **over), headers=h.headers(tenant)
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("model", body)


class TestCrud:
    def test_create_returns_the_model(self, h):
        body = _create(h)
        assert body["name"] == "gpt-test"
        assert body["model_name"] == "gpt-4o-mini"

    def test_a_model_without_any_provider_is_rejected(self, h):
        r = h.client.post(
            "/v1/admin/llms",
            json={"name": "orphan", "model_name": "gpt-4o-mini", "mode": "chat"},
            headers=h.headers(),
        )
        assert r.status_code in (400, 422), r.text

    def test_get_by_id_round_trips(self, h):
        mid = _create(h)["id"]
        r = h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers())
        assert r.status_code == 200, r.text
        assert r.json().get("model", r.json())["id"] == mid

    def test_listing_contains_the_model(self, h):
        mid = _create(h)["id"]
        r = h.client.get("/v1/admin/llms", headers=h.headers())
        assert r.status_code == 200, r.text
        payload = r.json()
        models = payload.get("models", payload if isinstance(payload, list) else [])
        if isinstance(models, dict):
            models = list(models.values())
        assert any(m["id"] == mid for m in models)

    def test_patch_updates_description_and_flag(self, h):
        mid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/llms/{mid}",
            json={"description": "cheap fast model", "enabled": False},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        model = r.json().get("model", r.json())
        assert model["enabled"] is False

    def test_delete_removes_the_model(self, h):
        mid = _create(h)["id"]
        r = h.client.delete(f"/v1/admin/llms/{mid}", headers=h.headers())
        assert r.status_code in (200, 204), r.text
        assert (
            h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers()).status_code
            == 404
        )

    def test_unknown_model_is_404(self, h):
        assert (
            h.client.get("/v1/admin/llms/nope", headers=h.headers()).status_code
            == 404
        )

    def test_copy_produces_an_independent_model(self, h):
        mid = _create(h)["id"]
        r = h.client.post(f"/v1/admin/llms/{mid}/copy", headers=h.headers())
        assert r.status_code in (200, 201), r.text
        copy = r.json().get("model", r.json())
        assert copy["id"] != mid


class TestGroupAssignment:
    def test_assign_then_remove_round_trip(self, h):
        mid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/llms/{mid}/groups/assign",
            json={"group_ids": ["group-default-t1"]},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.get(
            "/v1/admin/llms/by-group",
            params={"group_id": "group-default-t1"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.post(
            f"/v1/admin/llms/{mid}/groups/remove",
            json={"group_ids": ["group-default-t1"]},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_user_access_view_answers(self, h):
        _create(h)
        r = h.client.post(
            "/v1/admin/llms/user-access",
            json={"include_global": True},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestSearch:
    def test_search_finds_the_model(self, h):
        mid = _create(h, name="findable-model")["id"]
        r = h.client.post(
            "/v1/admin/llms/search",
            json={"name": "findable"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        models = payload.get("models", payload.get("results", []))
        if isinstance(models, dict):
            models = list(models.values())
        assert any(m["id"] == mid for m in models)

    def test_quick_search_answers(self, h):
        _create(h)
        r = h.client.get(
            "/v1/admin/llms/search/quick",
            params={"q": "gpt"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestIsolation:
    def test_other_tenant_cannot_read_or_delete(self, h):
        mid = _create(h, tenant="t1")["id"]
        assert (
            h.client.get(
                f"/v1/admin/llms/{mid}", headers=h.headers("t2")
            ).status_code
            == 404
        )
        assert (
            h.client.delete(
                f"/v1/admin/llms/{mid}", headers=h.headers("t2")
            ).status_code
            == 404
        )

    def test_cannot_create_against_a_foreign_provider(self, h):
        r = h.client.post(
            "/v1/admin/llms",
            json=_mk("sneaky", provider_id="prov-t2"),
            headers=h.headers("t1"),
        )
        assert r.status_code in (400, 404, 422), r.text


class TestCircuitBreakerSettings:
    """`allowed_fails` and `cooldown_time`: the model's own circuit breaker.
    The gateway uses the pair when both are set and its defaults otherwise,
    so one without the other is refused."""

    def _stored(self, h, mid):
        from app.database.models.llm_model import LLModel

        s = h.Factory()
        try:
            m = s.get(LLModel, mid)
            return m.allowed_fails, m.cooldown_time
        finally:
            s.close()

    def _get(self, h, mid):
        body = h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers()).json()
        body = body.get("model", body)
        return body["allowed_fails"], body["cooldown_time"]

    def test_a_model_created_without_them_has_none(self, h):
        body = _create(h)
        assert (body["allowed_fails"], body["cooldown_time"]) == (None, None)
        assert self._stored(h, body["id"]) == (None, None)

    def test_the_pair_is_saved_on_create_and_read_back(self, h):
        body = _create(h, allowed_fails=3, cooldown_time=45)
        assert (body["allowed_fails"], body["cooldown_time"]) == (3, 45)
        assert self._stored(h, body["id"]) == (3, 45)
        assert self._get(h, body["id"]) == (3, 45)

    @pytest.mark.parametrize("pair", [{"allowed_fails": 3}, {"cooldown_time": 45}])
    def test_one_without_the_other_is_refused_on_create(self, h, pair):
        r = h.client.post("/v1/admin/llms", json=_mk(**pair), headers=h.headers())
        assert r.status_code == 422, r.text
        assert "allowed_fails" in r.text and "cooldown_time" in r.text

    @pytest.mark.parametrize(
        "pair",
        [
            {"allowed_fails": 0, "cooldown_time": 45},
            {"allowed_fails": 101, "cooldown_time": 45},
            {"allowed_fails": 3, "cooldown_time": 0},
            {"allowed_fails": 3, "cooldown_time": 3601},
            {"allowed_fails": 1.5, "cooldown_time": 45},
        ],
    )
    def test_out_of_range_is_refused(self, h, pair):
        r = h.client.post("/v1/admin/llms", json=_mk(**pair), headers=h.headers())
        assert r.status_code == 422, r.text
        mid = _create(h, name="ranged")["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json=pair, headers=h.headers())
        assert r.status_code == 422, r.text
        assert self._stored(h, mid) == (None, None)

    def test_the_pair_is_set_changed_and_cleared_on_update(self, h):
        mid = _create(h)["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"allowed_fails": 2, "cooldown_time": 30}, headers=h.headers())
        assert r.status_code == 200, r.text
        assert self._stored(h, mid) == (2, 30) and self._get(h, mid) == (2, 30)
        # One of a stored pair may change alone.
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"cooldown_time": 90}, headers=h.headers())
        assert r.status_code == 200, r.text
        assert self._stored(h, mid) == (2, 90)
        # An update that names neither leaves them.
        r = h.client.patch(f"/v1/admin/llms/{mid}", json={"description": "d"}, headers=h.headers())
        assert r.status_code == 200 and self._stored(h, mid) == (2, 90)
        r = h.client.patch(
            f"/v1/admin/llms/{mid}", json={"allowed_fails": None, "cooldown_time": None}, headers=h.headers()
        )
        assert r.status_code == 200, r.text
        assert self._stored(h, mid) == (None, None)

    @pytest.mark.parametrize("body", [{"allowed_fails": 2}, {"cooldown_time": 30}])
    def test_an_update_that_would_leave_one_alone_is_refused(self, h, body):
        mid = _create(h)["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json=body, headers=h.headers())
        assert r.status_code == 422, r.text
        assert self._stored(h, mid) == (None, None)

    @pytest.mark.parametrize("body", [{"allowed_fails": None}, {"cooldown_time": None}])
    def test_clearing_one_of_a_stored_pair_is_refused(self, h, body):
        mid = _create(h, allowed_fails=2, cooldown_time=30)["id"]
        r = h.client.patch(f"/v1/admin/llms/{mid}", json=body, headers=h.headers())
        assert r.status_code == 422, r.text
        assert self._stored(h, mid) == (2, 30)
