# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM routing groups over HTTP: CRUD with the virtual model it maintains, tenant
isolation, permissions, and the status and deployment views.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.llm_routing_groups import create_llm_router_admin_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.services.llm.router.llm_router_service import LLMRouterService
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/llm-router"
_n = {"i": 0}


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("test-routing-groups")
    for url in throwaway_pg_database("routing_groups"):
        harness = build_admin_harness(
            [create_llm_router_admin_router(LLMRouterService(logger=log), log)], database_url=url
        )

        def seed(s):
            for tid in ("t1", "t2"):
                s.add(LlmProvider(id=f"prov-{tid}", tenant_id=tid, key="openai", display_name="OpenAI"))
                s.flush()
                s.add(LLModel(id=f"member-{tid}", tenant_id=tid, provider_id=f"prov-{tid}", name=f"member-{tid}",
                              model_name="gpt", mode="chat", enabled=True))

        harness.seed(seed)
        yield harness
        harness.close()


def _group(h, tenant="t1", **over) -> str:
    _n["i"] += 1
    gid = over.pop("id", f"rg{_n['i']}")
    r = h.client.post(f"{BASE}/routing-groups", json={"id": gid, "name": gid, "mode": "chat", **over}, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    return gid


def _row(h, gid):
    s = h.Factory()
    try:
        g = s.get(LLMRoutingGroup, gid)
        return None if g is None else {"name": g.name, "tenant_id": g.tenant_id, "enabled": g.enabled, "rpm": g.rpm_limit}
    finally:
        s.close()


def _virtual(h, gid):
    s = h.Factory()
    try:
        v = s.get(LLModel, f"rg-{gid}")
        return None if v is None else {"name": v.name, "enabled": v.enabled, "tenant_id": v.tenant_id, "mode": v.mode}
    finally:
        s.close()


class TestIsolation:
    @pytest.mark.parametrize(
        "method,suffix,kw",
        [
            ("get", "", {}),
            ("patch", "", {"json": {"name": "stolen"}}),
            ("delete", "", {}),
            ("get", "/tiers", {}),
        ],
    )
    def test_another_tenants_group_looks_absent(self, h, method, suffix, kw):
        gid = _group(h, "t2")
        before = _row(h, gid)
        r = getattr(h.client, method)(f"{BASE}/routing-groups/{gid}{suffix}", headers=h.headers("t1"), **kw)
        assert r.status_code == 404, r.text
        assert _row(h, gid) == before

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _group(h), _group(h, "t2")
        r = h.client.get(f"{BASE}/routing-groups", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        ids = {g["id"] for g in r.json()["routing_groups"]}
        assert mine in ids and theirs not in ids

    def test_a_group_id_taken_in_another_tenant_cannot_be_reused(self, h):
        gid = _group(h, "t2")
        r = h.client.post(
            f"{BASE}/routing-groups", json={"id": gid, "name": "x", "mode": "chat"}, headers=h.headers("t1")
        )
        assert r.status_code in (400, 409), r.text
        assert _row(h, gid)["tenant_id"] == "t2"

    def test_deployments_do_not_cross_tenants(self, h):
        r = h.client.get(f"{BASE}/deployments", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "member-t2" not in r.text
        assert h.client.get(f"{BASE}/deployments/member-t2", headers=h.headers("t1")).status_code == 404


class TestPermissions:
    @pytest.mark.parametrize(
        "method,path,kw,held",
        [
            ("post", "/routing-groups", {"json": {"id": "x", "name": "x", "mode": "chat"}}, Permission.LLM_ROUTER_READ),
            ("patch", "/routing-groups/x", {"json": {"name": "y"}}, Permission.LLM_ROUTER_READ),
            ("delete", "/routing-groups/x", {}, Permission.LLM_ROUTER_READ),
            ("get", "/routing-groups", {}, Permission.GUARDRAIL_READ),
            ("get", "/status", {}, Permission.GUARDRAIL_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, path, kw, held):
        limited = h.limited_headers("t1", [held])
        assert getattr(h.client, method)(f"{BASE}{path}", headers=limited, **kw).status_code == 403

    def test_anonymous_is_refused(self, h):
        assert h.client.get(f"{BASE}/routing-groups").status_code in (401, 403)


class TestCrud:
    def test_create_makes_the_virtual_model(self, h):
        gid = _group(h, description="d", routing_strategy="simple-shuffle", rpm_limit=10)
        assert _virtual(h, gid) == {"name": f"[Routing Group] {gid}", "enabled": True, "tenant_id": "t1", "mode": "routing"}
        got = h.client.get(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1"))
        assert got.status_code == 200, got.text
        assert got.json()["rpm_limit"] == 10

    def test_duplicate_id_is_refused(self, h):
        gid = _group(h)
        r = h.client.post(f"{BASE}/routing-groups", json={"id": gid, "name": "y", "mode": "chat"}, headers=h.headers("t1"))
        assert r.status_code in (400, 409), r.text

    def test_update_reaches_the_virtual_model(self, h):
        gid = _group(h)
        r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"name": "renamed", "enabled": False, "routing_strategy": "latency-based-routing"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        v = _virtual(h, gid)
        assert v["name"] == "[Routing Group] renamed" and v["enabled"] is False

    def test_delete_removes_group_and_virtual_model(self, h):
        gid = _group(h)
        r = h.client.delete(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, gid) is None and _virtual(h, gid) is None

    def test_delete_with_members_is_refused_or_detaches_them(self, h):
        gid = _group(h)

        def join(s):
            s.get(LLModel, "member-t1").routing_group_id = gid

        def leave(s):
            s.get(LLModel, "member-t1").routing_group_id = None

        h.seed(join)
        try:
            r = h.client.delete(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1"))
            assert r.status_code in (200, 400, 409), r.text
            s = h.Factory()
            try:
                member = s.get(LLModel, "member-t1")
                assert member is not None
                if r.status_code == 200:
                    assert member.routing_group_id is None
                else:
                    assert member.routing_group_id == gid
            finally:
                s.close()
        finally:
            h.seed(leave)

    def test_mode_is_locked_while_members_are_assigned(self, h):
        gid = _group(h)

        def join(s):
            s.get(LLModel, "member-t1").routing_group_id = gid

        def leave(s):
            s.get(LLModel, "member-t1").routing_group_id = None

        h.seed(join)
        try:
            r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"mode": "embedding"}, headers=h.headers("t1"))
            assert r.status_code in (400, 404, 409), r.text
        finally:
            h.seed(leave)

    @pytest.mark.parametrize("path", ["/routing-groups/nope", "/deployments/nope"])
    def test_unknown_is_404(self, h, path):
        assert h.client.get(f"{BASE}{path}", headers=h.headers("t1")).status_code == 404

    def test_a_bad_strategy_is_refused(self, h):
        r = h.client.post(
            f"{BASE}/routing-groups", json={"id": "bad-strategy", "name": "x", "mode": "chat", "routing_strategy": "yolo"},
            headers=h.headers("t1"),
        )
        assert r.status_code == 422


class TestTiers:
    def _with_member(self, h, gid, model="member-t1"):
        def join(s):
            s.get(LLModel, model).routing_group_id = gid

        h.seed(join)

    def test_a_member_can_be_mapped_and_cleared(self, h):
        gid = _group(h)
        self._with_member(h, gid)
        path = f"{BASE}/routing-groups/{gid}/tiers"
        try:
            r = h.client.put(path, json={"tiers": [{"tier": "simple", "llm_model_id": "member-t1"}]}, headers=h.headers("t1"))
            assert r.status_code == 200, r.text
            assert [t["llm_model_id"] for t in h.client.get(path, headers=h.headers("t1")).json()["tiers"]] == ["member-t1"]
            assert h.client.put(path, json={"tiers": []}, headers=h.headers("t1")).status_code == 200
            assert h.client.get(path, headers=h.headers("t1")).json()["tiers"] == []
        finally:
            self._with_member(h, None)

    def test_a_model_outside_the_group_cannot_be_mapped(self, h):
        """The governance boundary: a tier map may narrow the group, never reach outside it."""
        gid = _group(h)
        path = f"{BASE}/routing-groups/{gid}/tiers"
        for model in ("member-t1", "member-t2", "nope"):
            r = h.client.put(path, json={"tiers": [{"tier": "simple", "llm_model_id": model}]}, headers=h.headers("t1"))
            assert r.status_code in (400, 404, 422), r.text
        assert h.client.get(path, headers=h.headers("t1")).json()["tiers"] == []

    def test_another_tenants_group_is_refused(self, h):
        gid = _group(h, "t2")
        r = h.client.put(f"{BASE}/routing-groups/{gid}/tiers", json={"tiers": []}, headers=h.headers("t1"))
        assert r.status_code == 404


class TestViews:
    @pytest.mark.parametrize("path", ["/status", "/deployments", "/routing-groups?enabled_only=true"])
    def test_each_view_answers(self, h, path):
        _group(h)
        r = h.client.get(f"{BASE}{path}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text

    def test_a_disabled_group_leaves_the_enabled_only_list(self, h):
        gid = _group(h)
        h.client.patch(f"{BASE}/routing-groups/{gid}", json={"enabled": False}, headers=h.headers("t1"))
        ids = {g["id"] for g in h.client.get(f"{BASE}/routing-groups?enabled_only=true", headers=h.headers("t1")).json()["routing_groups"]}
        assert gid not in ids


def test_the_routing_test_sends_a_real_request_and_takes_the_test_permission(h):
    """It reaches a provider and is billed: reading the router's
    configuration does not open it."""
    from app.core.admin_user.permissions import Permission

    reader = h.limited_headers("t1", [Permission.LLM_ROUTER_READ])
    r = h.client.post("/v1/admin/llm-router/test", json={"routing_group_id": "nope"}, headers=reader)
    assert r.status_code == 403, r.text
    assert "llm:router:test" in r.text

    operation = h.app.openapi()["paths"]["/v1/admin/llm-router/test"]["post"]
    assert "without making actual API call" not in operation["description"]
    assert "real" in operation["description"] and "llm:router:test" in operation["description"]


class TestStreamTimeout:
    """The longest a streamed call waits for the first chunk of the member
    it was routed to: on create, on update and in what is read back."""

    def _stored(self, h, gid):
        s = h.Factory()
        try:
            return s.get(LLMRoutingGroup, gid).stream_timeout
        finally:
            s.close()

    def test_it_is_saved_on_create_and_read_back(self, h):
        gid = _group(h, stream_timeout=12.5)
        assert self._stored(h, gid) == 12.5
        got = h.client.get(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1"))
        assert got.json()["stream_timeout"] == 12.5

    def test_a_group_created_without_one_has_none(self, h):
        gid = _group(h)
        assert self._stored(h, gid) is None
        got = h.client.get(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1"))
        assert got.json()["stream_timeout"] is None

    def test_it_is_set_and_cleared_on_update(self, h):
        gid = _group(h)
        r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"stream_timeout": 3}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["stream_timeout"] == 3 and self._stored(h, gid) == 3
        # An update that does not name it leaves it.
        r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"name": "other"}, headers=h.headers("t1"))
        assert r.status_code == 200 and self._stored(h, gid) == 3
        r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"stream_timeout": None}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["stream_timeout"] is None and self._stored(h, gid) is None

    @pytest.mark.parametrize("value", [0, -1, 1800.5, 1e9, "soon"])
    def test_it_is_a_positive_number_of_seconds_up_to_the_ceiling(self, h, value):
        r = h.client.post(
            f"{BASE}/routing-groups",
            json={"id": "rg-bad-timeout", "name": "x", "mode": "chat", "stream_timeout": value},
            headers=h.headers("t1"),
        )
        assert r.status_code == 422, r.text
        gid = _group(h)
        r = h.client.patch(f"{BASE}/routing-groups/{gid}", json={"stream_timeout": value}, headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert self._stored(h, gid) is None

    @pytest.mark.parametrize("value", [0.001, 1, 1800])
    def test_the_bounds_are_accepted(self, h, value):
        gid = _group(h, stream_timeout=value)
        assert self._stored(h, gid) == value


def test_create_saves_every_field_it_accepts(h):
    """A field the create body accepts is stored, as the update stores it."""
    other = _group(h)
    sent = {
        "content_policy_fallbacks": [other],
        "default_fallbacks": [other],
        "max_fallbacks": 4,
        "complexity_floor_tier": "medium",
        "complexity_config": {"tier_boundaries": {"simple_medium": 0.2}},
    }
    gid = _group(h, **sent)
    s = h.Factory()
    try:
        g = s.get(LLMRoutingGroup, gid)
        stored = {k: getattr(g, k) for k in sent}
    finally:
        s.close()
    assert stored == sent
    got = h.client.get(f"{BASE}/routing-groups/{gid}", headers=h.headers("t1")).json()
    assert {k: got[k] for k in sent} == sent
