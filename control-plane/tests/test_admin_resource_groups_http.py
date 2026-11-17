# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the resource groups admin API
(/v1/admin/resource-groups) — the unit of governance.

This file walks the core surfaces of the 82 routes. What is worth defending:

  * **The hierarchy is real**: children/ancestors/descendants reflect
    parent_group_id, and the tree views agree with them.
  * **Resource binding round-trips** — attaching an LLM model to a group is
    THE access-granting act of the platform.
  * **Effective-* views answer** — they are what the enforcement path
    reads, resolved through inheritance.
  * **API keys under a group have a working lifecycle** (create, disable,
    enable, revoke).
  * **Tenant isolation everywhere.**
"""

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_model import LLModel
from app.services.usage_limit_enforcer import UsageLimitEnforcer


@pytest.fixture()
def h():
    log = BrutorLogger("test-resource-groups")
    harness = build_admin_harness(
        [create_resource_groups_router(UsageLimitEnforcer(logger=log), log)],
        logger_name="test-resource-groups",
    )

    def fn(s):
        s.add(
            LlmProvider(
                id="prov-t1", tenant_id="t1", key="openai", display_name="OpenAI"
            )
        )
        s.flush()
        s.add(
            LLModel(
                id="llm-t1-a",
                tenant_id="t1",
                name="gpt-test",
                provider_id="prov-t1",
                model_name="gpt-4o-mini",
                enabled=True,
            )
        )

    harness.seed(fn)
    yield harness
    harness.close()


def _create(h, tenant="t1", name="sales", **over):
    body = {"name": name, "description": f"{name} group"}
    body.update(over)
    r = h.client.post(
        "/v1/admin/resource-groups", json=body, headers=h.headers(tenant)
    )
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("group", payload)


class TestCrud:
    def test_create_returns_the_group(self, h):
        g = _create(h)
        assert g["name"] == "sales"

    def test_get_by_id_round_trips(self, h):
        gid = _create(h)["id"]
        r = h.client.get(f"/v1/admin/resource-groups/{gid}", headers=h.headers())
        assert r.status_code == 200, r.text
        assert r.json().get("group", r.json())["id"] == gid

    def test_listing_contains_it(self, h):
        gid = _create(h)["id"]
        r = h.client.get("/v1/admin/resource-groups", headers=h.headers())
        assert r.status_code == 200, r.text
        payload = r.json()
        groups = payload.get("groups", payload if isinstance(payload, list) else [])
        assert any(g["id"] == gid for g in groups)

    def test_patch_updates_description(self, h):
        gid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}",
            json={"description": "the EMEA sales group"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert (
            r.json().get("group", r.json())["description"]
            == "the EMEA sales group"
        )

    def test_delete_removes_it(self, h):
        gid = _create(h)["id"]
        r = h.client.delete(
            f"/v1/admin/resource-groups/{gid}", headers=h.headers()
        )
        assert r.status_code in (200, 204), r.text
        assert (
            h.client.get(
                f"/v1/admin/resource-groups/{gid}", headers=h.headers()
            ).status_code
            == 404
        )

    def test_unknown_group_is_404(self, h):
        r = h.client.get(
            "/v1/admin/resource-groups/nope", headers=h.headers()
        )
        assert r.status_code == 404

    def test_duplicate_name_is_rejected(self, h):
        _create(h, name="unique-team")
        r = h.client.post(
            "/v1/admin/resource-groups",
            json={"name": "unique-team"},
            headers=h.headers(),
        )
        assert r.status_code == 409, r.text


class TestHierarchy:
    def test_child_creation_and_navigation(self, h):
        parent = _create(h, name="emea")["id"]
        child = _create(h, name="emea-north", parent_group_id=parent)["id"]

        r = h.client.get(
            f"/v1/admin/resource-groups/{parent}/children", headers=h.headers()
        )
        assert r.status_code == 200, r.text
        kids = r.json()["descendants"]
        assert any(g["id"] == child for g in kids)

        r = h.client.get(
            f"/v1/admin/resource-groups/{child}/ancestors", headers=h.headers()
        )
        assert r.status_code == 200, r.text

        r = h.client.get(
            f"/v1/admin/resource-groups/{parent}/descendants", headers=h.headers()
        )
        assert r.status_code == 200, r.text

    def test_static_views_answer(self, h):
        _create(h, name="viewed")
        for path in ("/roots", "/tree", "/hierarchy-tree", "/stats",
                     "/by-type/custom", "/usage-alerts"):
            r = h.client.get(
                f"/v1/admin/resource-groups{path}", headers=h.headers()
            )
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:150]}"

    def test_search_answers(self, h):
        _create(h, name="findable-group")
        r = h.client.get(
            "/v1/admin/resource-groups/search",
            params={"q": "findable"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_a_group_cannot_parent_itself(self, h):
        gid = _create(h, name="loop")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}",
            json={"parent_group_id": gid},
            headers=h.headers(),
        )
        assert r.status_code in (400, 422), r.text


class TestModelBinding:
    def test_attach_then_detach_a_model(self, h):
        gid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/resource-groups/{gid}/llm-models",
            json={"llm_model_id": "llm-t1-a"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text

        r = h.client.get(
            f"/v1/admin/resource-groups/{gid}/effective-llm-models",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.delete(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a",
            headers=h.headers(),
        )
        assert r.status_code in (200, 204), r.text

    def test_effective_views_answer(self, h):
        gid = _create(h)["id"]
        for what in ("llm-models", "server-configs", "guardrails",
                     "agent-skills", "agent-cards"):
            r = h.client.get(
                f"/v1/admin/resource-groups/{gid}/effective-{what}",
                headers=h.headers(),
            )
            assert r.status_code == 200, f"{what}: {r.status_code} {r.text[:150]}"


class TestLimits:
    def test_llm_global_limits_round_trip(self, h):
        gid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/resource-groups/{gid}/llm-global-limits",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-global-limits",
            json={"llm_global_limits": {"requests_per_minute": 60}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_group_limits_view_answers(self, h):
        gid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/resource-groups/{gid}/limits", headers=h.headers()
        )
        assert r.status_code == 200, r.text


class TestGovernanceInheritance:
    """A child group may tighten inherited policy, never loosen it.

    Governance used to validate only the shape of the blob, so a child
    under an organisation capped at temperature 1.0 could save 1.4. The
    runtime discarded it (most-restrictive-wins), which is exactly why it
    must be refused: the console would otherwise show a policy the platform
    does not honour, and it would become real the day the parent relaxed.
    """

    def _parent_and_child(self, h, governance):
        parent = _create(h, name="org")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{parent['id']}/llm-global-governance",
            json={"governance": governance},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        child = _create(h, name="dept", parent_group_id=parent["id"])
        return parent, child

    def test_a_looser_temperature_is_refused(self, h):
        _, child = self._parent_and_child(h, {"temperature": {"max": 1.0}})
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/llm-global-governance",
            json={"governance": {"temperature": {"max": 1.4}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "Max Temperature" in detail
        assert "1.4" in detail and "1.0" in detail

    def test_a_tighter_temperature_is_stored(self, h):
        _, child = self._parent_and_child(h, {"temperature": {"max": 1.0}})
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/llm-global-governance",
            json={"governance": {"temperature": {"max": 0.4}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["llm_global_governance"]["temperature"]["max"] == 0.4

    def test_a_root_group_may_set_what_it_likes(self, h):
        gid = _create(h, name="root-org")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-global-governance",
            json={"governance": {"temperature": {"max": 2.0}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_a_looser_enforce_mode_is_refused(self, h):
        _, child = self._parent_and_child(h, {"temperature": {"enforce": "reject"}})
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/llm-global-governance",
            json={"governance": {"temperature": {"enforce": "clamp"}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "Enforce Mode" in r.json()["detail"]

    def test_a_looser_mcp_default_access_is_refused(self, h):
        parent = _create(h, name="mcp-org")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{parent['id']}/mcp-global-governance",
            json={"governance": {"default_tool_access": "approval_required"}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        child = _create(h, name="mcp-dept", parent_group_id=parent["id"])
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/mcp-global-governance",
            json={"governance": {"default_tool_access": "enabled"}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "Default Tool Access" in r.json()["detail"]


class TestCapacityReachesDeepGroups:
    """Capacity used to be computed against the immediate parent only, so an
    estate that sets its budget once at the organisation reported none at
    all for the AI systems three levels down."""

    def test_a_grandchild_sees_the_organisations_budget(self, h):
        org = _create(h, name="cap-org")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 200}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        dept = _create(h, name="cap-dept", parent_group_id=org["id"])
        system = _create(h, name="cap-system", parent_group_id=dept["id"])

        r = h.client.get(
            f"/v1/admin/resource-groups/{system['id']}/llm-global-limits",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inherited_llm_limits"]["budget"]["daily_limit_usd"] == 200
        capacity = body["capacity"]
        assert capacity is not None, "an unset parent must not hide the ancestor's budget"
        assert capacity["parent_name"] == "cap-org"
        assert capacity["parent_is_immediate"] is False


class TestPerItemCeilings:
    """A per-model, per-server or per-skill value looser than the group's
    effective ceiling is discarded by the restrictive merge, so it must be
    refused rather than stored — the same rule the group-wide endpoints have
    always applied, one level further down."""

    def _group_with_llm_ceiling(self, h):
        gid = _create(h, name="ceil-grp")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 100}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        r = h.client.post(
            f"/v1/admin/resource-groups/{gid}/llm-models",
            json={"llm_model_id": "llm-t1-a"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        return gid

    def test_a_per_model_limit_above_the_group_ceiling_is_refused(self, h):
        gid = self._group_with_llm_ceiling(h)
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a/limits",
            json={"limits": {"budget": {"daily_limit_usd": 500}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "per-model" in detail and "500" in detail and "100" in detail

    def test_a_per_model_limit_within_the_group_ceiling_is_stored(self, h):
        gid = self._group_with_llm_ceiling(h)
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a/limits",
            json={"limits": {"budget": {"daily_limit_usd": 40}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["limits"]["budget"]["daily_limit_usd"] == 40

    def test_a_group_with_no_ceiling_accepts_any_per_model_limit(self, h):
        gid = _create(h, name="ceil-free")["id"]
        r = h.client.post(
            f"/v1/admin/resource-groups/{gid}/llm-models",
            json={"llm_model_id": "llm-t1-a"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a/limits",
            json={"limits": {"budget": {"daily_limit_usd": 9999}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_a_per_model_policy_looser_than_the_group_is_refused(self, h):
        gid = _create(h, name="ceil-pol")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-global-governance",
            json={"governance": {"temperature": {"max": 0.5}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        r = h.client.post(
            f"/v1/admin/resource-groups/{gid}/llm-models",
            json={"llm_model_id": "llm-t1-a"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a/governance",
            json={"governance": {"temperature": {"max": 1.5}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "Max Temperature" in r.json()["detail"]

    def test_a_per_model_policy_tighter_than_the_group_is_stored(self, h):
        gid = _create(h, name="ceil-pol-ok")["id"]
        h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-global-governance",
            json={"governance": {"temperature": {"max": 0.9}}},
            headers=h.headers(),
        )
        h.client.post(
            f"/v1/admin/resource-groups/{gid}/llm-models",
            json={"llm_model_id": "llm-t1-a"},
            headers=h.headers(),
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/llm-models/llm-t1-a/governance",
            json={"governance": {"temperature": {"max": 0.2}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestMediaInheritance:
    """Media was the one surface with no inheritance in the control plane at
    all, though the proxy has always merged it across the chain — so the
    console reported no inherited media policy while the runtime enforced
    one."""

    def _org_and_child(self, h, limits=None, governance=None):
        org = _create(h, name="media-org")
        if limits is not None:
            r = h.client.patch(
                f"/v1/admin/resource-groups/{org['id']}/media-global-limits",
                json={"media_global_limits": limits},
                headers=h.headers(),
            )
            assert r.status_code == 200, r.text
        if governance is not None:
            r = h.client.patch(
                f"/v1/admin/resource-groups/{org['id']}/media-global-governance",
                json={"media_global_governance": governance},
                headers=h.headers(),
            )
            assert r.status_code == 200, r.text
        child = _create(h, name="media-child", parent_group_id=org["id"])
        return org, child

    def test_a_child_sees_the_inherited_media_limits(self, h):
        _, child = self._org_and_child(
            h, limits={"image_generation": {"daily_limit": 100}}
        )
        r = h.client.get(
            f"/v1/admin/resource-groups/{child['id']}/media-global-limits",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inherited_media_limits"]["image_generation"]["daily_limit"] == 100
        assert body["effective_media_limits"]["image_generation"]["daily_limit"] == 100

    def test_a_looser_media_limit_is_refused(self, h):
        _, child = self._org_and_child(
            h, limits={"image_generation": {"daily_limit": 100}}
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/media-global-limits",
            json={"media_global_limits": {"image_generation": {"daily_limit": 500}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "less restrictive" in r.json()["detail"]

    def test_a_child_sees_the_inherited_media_policy(self, h):
        _, child = self._org_and_child(
            h, governance={"audio_speech": {"max_input_characters": 500}}
        )
        r = h.client.get(
            f"/v1/admin/resource-groups/{child['id']}/media-global-governance",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inherited_media_governance"]["audio_speech"]["max_input_characters"] == 500

    def test_a_looser_media_ceiling_is_refused(self, h):
        _, child = self._org_and_child(
            h, governance={"audio_speech": {"max_input_characters": 500}}
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/media-global-governance",
            json={"media_global_governance": {"audio_speech": {"max_input_characters": 5000}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "audio_speech.max_input_characters" in r.json()["detail"]

    def test_widening_an_inherited_whitelist_is_refused(self, h):
        _, child = self._org_and_child(
            h, governance={"audio_speech": {"voice_whitelist": ["alloy"]}}
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/media-global-governance",
            json={"media_global_governance": {"audio_speech": {"voice_whitelist": ["alloy", "echo"]}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "echo" in r.json()["detail"]

    def test_narrowing_an_inherited_whitelist_is_allowed(self, h):
        _, child = self._org_and_child(
            h, governance={"audio_speech": {"voice_whitelist": ["alloy", "echo"]}}
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/media-global-governance",
            json={"media_global_governance": {"audio_speech": {"voice_whitelist": ["alloy"]}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_lowering_an_inherited_floor_is_refused(self, h):
        """`speed_min` is the one media field where a HIGHER value is
        stricter."""
        _, child = self._org_and_child(
            h, governance={"audio_speech": {"speed_min": 0.8}}
        )
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/media-global-governance",
            json={"media_global_governance": {"audio_speech": {"speed_min": 0.2}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "inherited minimum" in r.json()["detail"]

    def test_a_root_group_may_set_any_media_policy(self, h):
        gid = _create(h, name="media-root")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/media-global-governance",
            json={"media_global_governance": {"audio_speech": {"max_input_characters": 99999}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


class TestHierarchyTreeGovernanceFlags:
    """The tree is where an operator forms their mental model of an estate;
    it used to carry nothing about where limits or policy live."""

    def test_flags_show_where_governance_lives(self, h):
        org = _create(h, name="tree-org")
        dept = _create(h, name="tree-dept", parent_group_id=org["id"])
        _create(h, name="tree-team", parent_group_id=dept["id"])
        h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 100}}},
            headers=h.headers(),
        )
        h.client.patch(
            f"/v1/admin/resource-groups/{dept['id']}/llm-global-governance",
            json={"governance": {"temperature": {"max": 0.5}}},
            headers=h.headers(),
        )

        r = h.client.get("/v1/admin/resource-groups/tree", headers=h.headers())
        assert r.status_code == 200, r.text

        nodes = {}

        def walk(items):
            for n in items:
                nodes[n["name"]] = n
                walk(n.get("children") or [])

        walk(r.json())

        assert nodes["tree-org"]["sets_own_limits"] is True
        assert nodes["tree-org"]["sets_own_policy"] is False
        assert nodes["tree-org"]["inherits_limits"] is False

        assert nodes["tree-dept"]["sets_own_limits"] is False
        assert nodes["tree-dept"]["sets_own_policy"] is True
        assert nodes["tree-dept"]["inherits_limits"] is True, "the org's budget binds it"
        assert nodes["tree-dept"]["inherits_policy"] is False

        assert nodes["tree-team"]["sets_own_limits"] is False
        assert nodes["tree-team"]["sets_own_policy"] is False
        assert nodes["tree-team"]["inherits_limits"] is True
        assert nodes["tree-team"]["inherits_policy"] is True, "the dept's policy binds it"

    def test_a_subtree_view_still_reports_what_binds_its_root(self, h):
        org = _create(h, name="sub-org")
        dept = _create(h, name="sub-dept", parent_group_id=org["id"])
        h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 50}}},
            headers=h.headers(),
        )
        r = h.client.get(
            f"/v1/admin/resource-groups/tree?root_group_id={dept['id']}",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        root = r.json()[0]
        assert root["name"] == "sub-dept"
        assert root["inherits_limits"] is True, (
            "a subtree view must not report a group as ungoverned because the "
            "group that governs it is out of frame"
        )


class TestSiblingSumUsesTheBudgetHolder:
    """The console's capacity warning and the API's refusal must measure
    against the same budget."""

    def test_children_of_a_distant_holder_are_summed(self, h):
        org = _create(h, name="sum-org")
        h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 100}}},
            headers=h.headers(),
        )
        first = _create(h, name="sum-a", parent_group_id=org["id"])
        second = _create(h, name="sum-b", parent_group_id=org["id"])
        r = h.client.patch(
            f"/v1/admin/resource-groups/{first['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 70}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

        # 70 already claimed of 100; asking for 50 would over-commit.
        r = h.client.patch(
            f"/v1/admin/resource-groups/{second['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 50}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        assert "exceeds parent limit" in r.json()["detail"]

        # 30 fits exactly.
        r = h.client.patch(
            f"/v1/admin/resource-groups/{second['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 30}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text

    def test_a_save_returns_the_capacity_that_is_left_after_it(self, h):
        """The console reads capacity straight off the save response; a
        stale figure there would invite the very over-commit the check
        refuses."""
        org = _create(h, name="fresh-org")
        h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 100}}},
            headers=h.headers(),
        )
        a = _create(h, name="fresh-a", parent_group_id=org["id"])
        b = _create(h, name="fresh-b", parent_group_id=org["id"])

        r = h.client.patch(
            f"/v1/admin/resource-groups/{a['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 25}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        # `a` is not counted against itself, so its own view still shows 100.
        assert r.json()["capacity"]["available"]["budget"]["daily_limit_usd"] == 100

        r = h.client.get(
            f"/v1/admin/resource-groups/{b['id']}/llm-global-limits",
            headers=h.headers(),
        )
        assert r.json()["capacity"]["available"]["budget"]["daily_limit_usd"] == 75, (
            "the sibling's fresh claim must show up immediately"
        )

    def test_the_capacity_shown_predicts_the_refusal(self, h):
        """The number the console renders is the one the API enforces."""
        org = _create(h, name="pred-org")
        h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 100}}},
            headers=h.headers(),
        )
        first = _create(h, name="pred-a", parent_group_id=org["id"])
        h.client.patch(
            f"/v1/admin/resource-groups/{first['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": 60}}},
            headers=h.headers(),
        )
        dept = _create(h, name="pred-dept", parent_group_id=org["id"])
        deep = _create(h, name="pred-deep", parent_group_id=dept["id"])

        r = h.client.get(
            f"/v1/admin/resource-groups/{deep['id']}/llm-global-limits",
            headers=h.headers(),
        )
        capacity = r.json()["capacity"]
        assert capacity["parent_name"] == "pred-org"
        assert capacity["parent_is_immediate"] is False
        available = capacity["available"]["budget"]["daily_limit_usd"]
        assert available == 40

        # One over the displayed capacity is refused...
        r = h.client.patch(
            f"/v1/admin/resource-groups/{deep['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": available + 1}}},
            headers=h.headers(),
        )
        assert r.status_code == 400, r.text
        # ...and exactly the displayed capacity is accepted.
        r = h.client.patch(
            f"/v1/admin/resource-groups/{deep['id']}/llm-global-limits",
            json={"llm_global_limits": {"budget": {"daily_limit_usd": available}}},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text


# Every group-wide surface that accepts a limits or governance document,
# with a value that is unambiguously LOOSER than its parent's and one that
# is unambiguously TIGHTER. The sweep exists so a new surface cannot ship
# with inheritance quietly unenforced: the audit that prompted this work
# found skill limits and media with no HTTP coverage at all, and media with
# no inheritance in the control plane whatsoever.
LIMIT_SURFACES = [
    ("llm-global-limits", "llm_global_limits",
     {"budget": {"daily_limit_usd": 100}}, {"budget": {"daily_limit_usd": 500}},
     {"budget": {"daily_limit_usd": 25}}),
    ("mcp-global-limits", "mcp_global_limits",
     {"frequency": {"max_calls_per_day": 100}}, {"frequency": {"max_calls_per_day": 500}},
     {"frequency": {"max_calls_per_day": 25}}),
    ("skill-global-limits", "skill_global_limits",
     {"execution": {"daily_limit": 100}}, {"execution": {"daily_limit": 500}},
     {"execution": {"daily_limit": 25}}),
    ("media-global-limits", "media_global_limits",
     {"image_generation": {"daily_limit": 100}}, {"image_generation": {"daily_limit": 500}},
     {"image_generation": {"daily_limit": 25}}),
]

GOVERNANCE_SURFACES = [
    ("llm-global-governance", "governance",
     {"temperature": {"max": 0.5}}, {"temperature": {"max": 1.5}}, {"temperature": {"max": 0.2}}),
    ("mcp-global-governance", "governance",
     {"default_tool_access": "disabled"}, {"default_tool_access": "enabled"},
     {"default_tool_access": "disabled"}),
    ("skill-global-governance", "governance",
     {"execution_mode": "disabled"}, {"execution_mode": "unrestricted"},
     {"execution_mode": "disabled"}),
    ("media-global-governance", "media_global_governance",
     {"audio_speech": {"max_input_characters": 500}},
     {"audio_speech": {"max_input_characters": 5000}},
     {"audio_speech": {"max_input_characters": 100}}),
]


class TestEverySurfaceEnforcesInheritance:
    """Whatever the document is called and whatever shape it has, a child
    may tighten it and may not loosen it."""

    def _parent_and_child(self, h, route, key, parent_doc, name):
        parent = _create(h, name=f"{name}-p")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{parent['id']}/{route}",
            json={key: parent_doc},
            headers=h.headers(),
        )
        assert r.status_code == 200, f"{route} parent write: {r.text}"
        child = _create(h, name=f"{name}-c", parent_group_id=parent["id"])
        return child

    @pytest.mark.parametrize(
        "route,key,parent_doc,looser,tighter",
        LIMIT_SURFACES + GOVERNANCE_SURFACES,
        ids=[s[0] for s in LIMIT_SURFACES + GOVERNANCE_SURFACES],
    )
    def test_a_looser_child_document_is_refused(
        self, h, route, key, parent_doc, looser, tighter
    ):
        child = self._parent_and_child(h, route, key, parent_doc, f"sw-{route[:6]}-l")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/{route}",
            json={key: looser},
            headers=h.headers(),
        )
        assert r.status_code == 400, f"{route} accepted a relaxation: {r.text}"

    @pytest.mark.parametrize(
        "route,key,parent_doc,looser,tighter",
        LIMIT_SURFACES + GOVERNANCE_SURFACES,
        ids=[s[0] for s in LIMIT_SURFACES + GOVERNANCE_SURFACES],
    )
    def test_a_tighter_child_document_is_stored(
        self, h, route, key, parent_doc, looser, tighter
    ):
        child = self._parent_and_child(h, route, key, parent_doc, f"sw-{route[:6]}-t")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{child['id']}/{route}",
            json={key: tighter},
            headers=h.headers(),
        )
        assert r.status_code == 200, f"{route} refused a tightening: {r.text}"

    @pytest.mark.parametrize(
        "route,key,parent_doc,looser,tighter",
        LIMIT_SURFACES + GOVERNANCE_SURFACES,
        ids=[s[0] for s in LIMIT_SURFACES + GOVERNANCE_SURFACES],
    )
    def test_a_root_group_may_set_whatever_it_likes(
        self, h, route, key, parent_doc, looser, tighter
    ):
        gid = _create(h, name=f"sw-root-{route[:8]}")["id"]
        r = h.client.patch(
            f"/v1/admin/resource-groups/{gid}/{route}",
            json={key: looser},
            headers=h.headers(),
        )
        assert r.status_code == 200, f"{route} constrained a root group: {r.text}"

    @pytest.mark.parametrize(
        "route,key,parent_doc,looser,tighter",
        LIMIT_SURFACES + GOVERNANCE_SURFACES,
        ids=[s[0] for s in LIMIT_SURFACES + GOVERNANCE_SURFACES],
    )
    def test_the_refusal_reaches_a_grandchild(
        self, h, route, key, parent_doc, looser, tighter
    ):
        """Inheritance is not a parent-only relationship — the estate that
        prompted this work sets its budget once at the organisation and
        leaves every department between it and an AI system empty."""
        org = _create(h, name=f"sw-gc-{route[:6]}-o")
        r = h.client.patch(
            f"/v1/admin/resource-groups/{org['id']}/{route}",
            json={key: parent_doc},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        mid = _create(h, name=f"sw-gc-{route[:6]}-m", parent_group_id=org["id"])
        leaf = _create(h, name=f"sw-gc-{route[:6]}-l", parent_group_id=mid["id"])
        r = h.client.patch(
            f"/v1/admin/resource-groups/{leaf['id']}/{route}",
            json={key: looser},
            headers=h.headers(),
        )
        assert r.status_code == 400, (
            f"{route}: a grandparent's document must still bind: {r.text}"
        )


class TestApiKeys:
    def test_key_lifecycle(self, h):
        gid = _create(h)["id"]
        r = h.client.post(
            f"/v1/admin/resource-groups/{gid}/api-keys",
            json={"name": "ci key"},
            headers=h.headers(),
        )
        assert r.status_code in (200, 201), r.text
        payload = r.json()
        key = payload.get("api_key", payload.get("key", payload))
        kid = key["id"] if isinstance(key, dict) else payload["id"]

        listing = h.client.get(
            f"/v1/admin/resource-groups/{gid}/api-keys", headers=h.headers()
        )
        assert listing.status_code == 200, listing.text

        for action in ("disable", "enable", "revoke"):
            r = h.client.post(
                f"/v1/admin/resource-groups/api-keys/{kid}/{action}",
                headers=h.headers(),
            )
            assert r.status_code == 200, f"{action}: {r.status_code} {r.text[:150]}"


class TestIsolation:
    def test_other_tenant_cannot_read_patch_or_delete(self, h):
        gid = _create(h, tenant="t1")["id"]
        assert (
            h.client.get(
                f"/v1/admin/resource-groups/{gid}", headers=h.headers("t2")
            ).status_code
            == 404
        )
        assert (
            h.client.patch(
                f"/v1/admin/resource-groups/{gid}",
                json={"description": "hostile"},
                headers=h.headers("t2"),
            ).status_code
            == 404
        )
        assert (
            h.client.delete(
                f"/v1/admin/resource-groups/{gid}", headers=h.headers("t2")
            ).status_code
            == 404
        )

    def test_cannot_parent_under_a_foreign_group(self, h):
        foreign = _create(h, tenant="t2", name="their-root")["id"]
        r = h.client.post(
            "/v1/admin/resource-groups",
            json={"name": "infiltrator", "parent_group_id": foreign},
            headers=h.headers("t1"),
        )
        assert r.status_code in (400, 404, 422), r.text


# ---------------------------------------------------------------------------
# The lifecycle gate has no back door on create either
# ---------------------------------------------------------------------------


class TestAiSystemLifecycleOnCreate:
    """PATCH refuses to move an AI System's `lifecycle_stage` (the evidence
    requirements live on POST /v1/admin/ai-systems/{id}/lifecycle), but POST
    used to write any stage verbatim, so a system could be born `active`
    with no contract and no approver."""

    @pytest.mark.parametrize("stage", ["approved", "active", "deprecated", "retired"])
    def test_an_ai_system_cannot_be_born_past_proposed(self, h, stage):
        r = h.client.post(
            "/v1/admin/resource-groups",
            json={"name": f"sys-{stage}", "group_type": "ai_system", "lifecycle_stage": stage},
            headers=h.headers(),
        )
        assert r.status_code == 409, r.text
        assert "/lifecycle" in r.text, "the refusal must point at the gated endpoint"
        # Nothing was written.
        r = h.client.get("/v1/admin/resource-groups", headers=h.headers())
        assert r.status_code == 200, r.text
        listing = r.json()
        groups = listing.get("groups", listing) if isinstance(listing, dict) else listing
        assert not any(g["name"] == f"sys-{stage}" for g in groups)

    def test_an_ai_system_may_be_born_proposed(self, h):
        g = _create(h, name="sys-proposed", group_type="ai_system", lifecycle_stage="proposed")
        assert g["group_type"] == "ai_system"
        assert g["lifecycle_stage"] == "proposed"

    def test_an_ai_system_with_no_stage_is_accepted(self, h):
        g = _create(h, name="sys-unstaged", group_type="ai_system")
        assert g["group_type"] == "ai_system"

    def test_other_group_types_keep_the_field_as_documentation(self, h):
        g = _create(h, name="team-active", group_type="team", lifecycle_stage="active")
        assert g["lifecycle_stage"] == "active"
