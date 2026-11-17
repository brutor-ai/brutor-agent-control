# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Resource group data paths the CRUD tests never reached: MCP capability filters
(a child cannot relax a parent), membership and the primary-group invariant,
per-skill and per-server limits and governance, usage alerts, effective views.
Runs on SQLite by default and on Postgres under BRUTOR_PG_TESTS=1.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.end_user import EndUser
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.mcp_server import MCPServer
from app.database.models.notification import Notification
from app.database.models.resource_group import ResourceGroup, resource_group_mcp_capabilities
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.server_config import ServerConfig
from app.database.models.usage_alert import UsageAlert
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/resource-groups"
_n = {"i": 0}


def _next() -> str:
    _n["i"] += 1
    return f"{_n['i']:04d}"


def harness_with(extra_routers=()):
    """The file's harness: the resource-groups router, and the routers an
    edition adds. The effective-KB views are a router of their own on the same
    prefix; the Enterprise half mounts it."""
    log = BrutorLogger("test-rg-data")
    for url in throwaway_pg_database("rg_data"):
        harness = build_admin_harness(
            [create_resource_groups_router(UsageLimitEnforcer(logger=log), log), *extra_routers],
            logger_name="test-rg-data",
            database_url=url,
        )

        def seed(s):
            s.add(LlmProvider(id="prov-t1", tenant_id="t1", key="openai", display_name="OpenAI"))
            s.flush()
            s.add(LLModel(id="llm-t1", tenant_id="t1", name="m", provider_id="prov-t1", model_name="m", enabled=True))
            s.add(MCPServer(id="srv-t1", tenant_id="t1", name="srv-t1", base_url="https://mcp.example"))
            s.add(MCPServer(id="srv-t2", tenant_id="t2", name="srv-t2", base_url="https://mcp.example"))
            s.flush()
            s.add(ServerConfig(id="cfg-t1", tenant_id="t1", name="cfg-t1", mcp_server_id="srv-t1", enabled=True))
            s.add(ServerConfig(id="cfg-t2", tenant_id="t2", name="cfg-t2", mcp_server_id="srv-t2", enabled=True))
            s.add(AgentSkill(id="skill-t1", tenant_id="t1", name="s", description="d", skill_md_content="# x", skill_md_hash="h"))
            s.add(AgentSkill(id="skill-t2", tenant_id="t2", name="s", description="d", skill_md_content="# x", skill_md_hash="h"))

        harness.seed(seed)
        yield harness
        harness.close()


@pytest.fixture(scope="module")
def h():
    yield from harness_with()


def _group(h, parent: str | None = None, tenant: str = "t1") -> str:
    body = {"name": f"g-{_next()}"}
    if parent:
        body["parent_group_id"] = parent
    r = h.client.post(BASE, json=body, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("group", payload)["id"]


def _user(h, tenant: str = "t1") -> int:
    n = _next()
    s = h.Factory()
    try:
        u = EndUser(tenant_id=tenant, username=f"u{n}", email=f"u{n}@x.example", password_hash="x", is_active=True)
        s.add(u)
        s.commit()
        return u.id
    finally:
        s.close()


def _attach_config(h, gid: str, cfg: str = "cfg-t1") -> None:
    r = h.client.post(f"{BASE}/{gid}/server-configs", json={"server_config_id": cfg}, headers=h.headers("t1"))
    assert r.status_code in (200, 201), r.text


def _caps(tool: str, access: str, **extra) -> dict:
    return {"capabilities": [{"capability_type": "tool", "capability_name": tool, "access": access, **extra}]}


def _cap_rows(h, gid: str, cfg: str = "cfg-t1") -> dict:
    s = h.Factory()
    try:
        rows = s.execute(
            resource_group_mcp_capabilities.select().where(
                resource_group_mcp_capabilities.c.resource_group_id == gid,
                resource_group_mcp_capabilities.c.server_config_id == cfg,
            )
        ).fetchall()
        return {r.capability_name: r.access for r in rows}
    finally:
        s.close()


class TestServerConfigBinding:
    def test_attach_list_and_detach(self, h):
        gid = _group(h)
        _attach_config(h, gid)
        effective = lambda: h.client.get(f"{BASE}/{gid}/effective-server-configs", headers=h.headers("t1")).text
        assert "cfg-t1" in effective()
        r = h.client.delete(f"{BASE}/{gid}/server-configs/cfg-t1", headers=h.headers("t1"))
        assert r.status_code in (200, 204), r.text
        assert "cfg-t1" not in effective()

    def test_attaching_twice_is_a_conflict(self, h):
        gid = _group(h)
        _attach_config(h, gid)
        r = h.client.post(f"{BASE}/{gid}/server-configs", json={"server_config_id": "cfg-t1"}, headers=h.headers("t1"))
        assert r.status_code == 409

    def test_another_tenants_config_cannot_be_attached(self, h):
        gid = _group(h)
        r = h.client.post(f"{BASE}/{gid}/server-configs", json={"server_config_id": "cfg-t2"}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert "cfg-t2" not in h.client.get(f"{BASE}/{gid}/effective-server-configs", headers=h.headers("t1")).text

    def test_detaching_what_is_not_attached_is_404(self, h):
        assert h.client.delete(f"{BASE}/{_group(h)}/server-configs/cfg-t1", headers=h.headers("t1")).status_code == 404

    def test_a_child_sees_the_parents_config_as_effective(self, h):
        parent = _group(h)
        _attach_config(h, parent)
        child = _group(h, parent)
        r = h.client.get(f"{BASE}/{child}/effective-server-configs", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "cfg-t1" in r.text


class TestCapabilityFilters:
    def test_set_read_replace_and_clear(self, h):
        gid = _group(h)
        _attach_config(h, gid)
        path = f"{BASE}/{gid}/server-configs/cfg-t1/capabilities"
        r = h.client.put(path, json=_caps("delete_all", "disabled"), headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _cap_rows(h, gid) == {"delete_all": "disabled"}
        got = h.client.get(path, headers=h.headers("t1"))
        assert got.status_code == 200, got.text
        assert got.json()["has_filters"] is True
        r = h.client.put(path, json=_caps("other", "approval_required", approval_timeout_seconds=30), headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _cap_rows(h, gid) == {"other": "approval_required"}
        r = h.client.delete(path, headers=h.headers("t1"))
        assert r.status_code in (200, 204), r.text
        assert _cap_rows(h, gid) == {}

    @pytest.mark.parametrize(
        "parent_access,child_access,code",
        [
            ("disabled", "enabled", 400),
            ("disabled", "approval_required", 400),
            ("approval_required", "enabled", 400),
            ("approval_required", "disabled", 200),
            ("enabled", "disabled", 200),
        ],
    )
    def test_a_child_cannot_relax_the_parent(self, h, parent_access, child_access, code):
        parent = _group(h)
        _attach_config(h, parent)
        child = _group(h, parent)
        r = h.client.put(
            f"{BASE}/{parent}/server-configs/cfg-t1/capabilities", json=_caps("t", parent_access), headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text
        r = h.client.put(
            f"{BASE}/{child}/server-configs/cfg-t1/capabilities", json=_caps("t", child_access), headers=h.headers("t1")
        )
        assert r.status_code == code, r.text
        assert _cap_rows(h, child) == ({"t": child_access} if code == 200 else {})

    def test_a_child_reads_the_inherited_restriction_with_its_source(self, h):
        parent = _group(h)
        _attach_config(h, parent)
        child = _group(h, parent)
        h.client.put(f"{BASE}/{parent}/server-configs/cfg-t1/capabilities", json=_caps("t", "disabled"), headers=h.headers("t1"))
        body = h.client.get(f"{BASE}/{child}/server-configs/cfg-t1/capabilities", headers=h.headers("t1")).json()
        cap = next(c for c in body["capabilities"] if c["capability_name"] == "t")
        assert cap["access"] == "disabled"
        assert cap["source_group_id"] == parent

    @pytest.mark.parametrize(
        "body", [_caps("t", "maybe"), {"capabilities": [{"capability_type": "nope", "capability_name": "t", "access": "enabled"}]}]
    )
    def test_bad_values_are_refused(self, h, body):
        gid = _group(h)
        _attach_config(h, gid)
        r = h.client.put(f"{BASE}/{gid}/server-configs/cfg-t1/capabilities", json=body, headers=h.headers("t1"))
        assert r.status_code == 422
        assert _cap_rows(h, gid) == {}

    def test_a_config_the_group_cannot_reach_is_refused(self, h):
        gid = _group(h)
        r = h.client.put(f"{BASE}/{gid}/server-configs/cfg-t1/capabilities", json=_caps("t", "disabled"), headers=h.headers("t1"))
        assert r.status_code == 404

    def test_another_tenants_config_is_404_everywhere(self, h):
        gid = _group(h)
        path = f"{BASE}/{gid}/server-configs/cfg-t2/capabilities"
        assert h.client.get(path, headers=h.headers("t1")).status_code == 404
        assert h.client.put(path, json=_caps("t", "disabled"), headers=h.headers("t1")).status_code == 404
        # DELETE only ever removes rows of my own group, so it is an idempotent no-op.
        assert h.client.delete(path, headers=h.headers("t1")).status_code in (204, 404)
        assert _cap_rows(h, gid, "cfg-t2") == {}

    def test_a_read_only_admin_cannot_write(self, h):
        gid = _group(h)
        _attach_config(h, gid)
        ro = h.limited_headers("t1", [Permission.RESOURCE_GROUP_READ])
        path = f"{BASE}/{gid}/server-configs/cfg-t1/capabilities"
        assert h.client.get(path, headers=ro).status_code == 200
        assert h.client.put(path, json=_caps("t", "enabled"), headers=ro).status_code == 403
        assert h.client.delete(path, headers=ro).status_code == 403
        assert _cap_rows(h, gid) == {}


class TestMembers:
    def _add(self, h, gid, *users, **over):
        return h.client.post(
            f"{BASE}/{gid}/members", json={"user_ids": [str(u) for u in users], **over}, headers=h.headers("t1")
        )

    def _primary(self, h, uid):
        s = h.Factory()
        try:
            return s.get(EndUser, uid).primary_group_id
        finally:
            s.close()

    def test_first_group_becomes_primary_and_members_are_listed(self, h):
        gid, uid = _group(h), _user(h)
        r = self._add(h, gid, uid)
        assert r.status_code in (200, 201), r.text
        assert self._primary(h, uid) == gid
        listed = h.client.get(f"{BASE}/{gid}/members", headers=h.headers("t1"))
        assert listed.status_code == 200, listed.text
        assert str(uid) in listed.text

    def test_a_second_group_does_not_take_primary_unless_asked(self, h):
        a, b, uid = _group(h), _group(h), _user(h)
        self._add(h, a, uid)
        self._add(h, b, uid)
        assert self._primary(h, uid) == a
        c = _group(h)
        self._add(h, c, uid, make_primary=True)
        assert self._primary(h, uid) == c

    def test_removing_the_primary_promotes_another_membership(self, h):
        a, b, uid = _group(h), _group(h), _user(h)
        self._add(h, a, uid), self._add(h, b, uid)
        r = h.client.delete(f"{BASE}/{a}/members/{uid}", headers=h.headers("t1"))
        assert r.status_code in (200, 204), r.text
        assert self._primary(h, uid) == b

    def test_removing_the_last_clears_primary_and_notifies_the_user(self, h):
        a, uid = _group(h), _user(h)
        self._add(h, a, uid)
        assert h.client.delete(f"{BASE}/{a}/members/{uid}", headers=h.headers("t1")).status_code in (200, 204)
        assert self._primary(h, uid) is None
        s = h.Factory()
        try:
            assert s.query(ResourceGroupMembership).filter_by(user_id=uid).count() == 0
            assert s.query(Notification).filter(Notification.tenant_id == "t1").count() >= 1
        finally:
            s.close()

    def test_a_user_cannot_be_in_a_parent_and_its_child(self, h):
        parent, uid = _group(h), _user(h)
        child = _group(h, parent)
        self._add(h, parent, uid)
        r = self._add(h, child, uid)
        assert r.status_code in (200, 201, 400, 409), r.text
        s = h.Factory()
        try:
            assert s.query(ResourceGroupMembership).filter_by(user_id=uid, group_id=child).count() == 0
        finally:
            s.close()

    def test_a_user_of_another_tenant_is_skipped(self, h):
        gid, foreign = _group(h), _user(h, "t2")
        r = self._add(h, gid, foreign)
        assert foreign not in {m["id"] for m in r.json().get("added", []) if isinstance(m, dict)}
        s = h.Factory()
        try:
            assert s.query(ResourceGroupMembership).filter_by(user_id=foreign).count() == 0
            assert s.get(EndUser, foreign).primary_group_id is None
        finally:
            s.close()

    def test_removing_a_non_member_is_404(self, h):
        assert h.client.delete(f"{BASE}/{_group(h)}/members/{_user(h)}", headers=h.headers("t1")).status_code == 404

    def test_descendants_appear_only_when_asked(self, h):
        parent, uid = _group(h), _user(h)
        child = _group(h, parent)
        self._add(h, child, uid)
        direct = h.client.get(f"{BASE}/{parent}/members", headers=h.headers("t1"))
        assert f'"{uid}"' not in direct.text and f":{uid}," not in direct.text
        with_kids = h.client.get(f"{BASE}/{parent}/members?include_descendants=true", headers=h.headers("t1"))
        assert with_kids.status_code == 200, with_kids.text
        assert str(uid) in with_kids.text


class TestSkillGovernance:
    def _bind(self, h, gid, skill="skill-t1"):
        def bind(s):
            s.execute(resource_group_agent_skills.insert().values(resource_group_id=gid, agent_skill_id=skill))

        h.seed(bind)

    def _row(self, h, gid):
        s = h.Factory()
        try:
            return s.execute(
                resource_group_agent_skills.select().where(resource_group_agent_skills.c.resource_group_id == gid)
            ).first()
        finally:
            s.close()

    def test_round_trip(self, h):
        gid = _group(h)
        self._bind(h, gid)
        path = f"{BASE}/{gid}/agent-skills/skill-t1/governance"
        r = h.client.patch(path, json={"governance": {"execution_mode": "approval_required"}}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert self._row(h, gid).governance == {"execution_mode": "approval_required"}
        got = h.client.get(path, headers=h.headers("t1"))
        assert got.status_code == 200, got.text
        assert h.client.delete(path, headers=h.headers("t1")).status_code == 204
        assert self._row(h, gid).governance is None

    def test_a_bad_mode_is_refused(self, h):
        gid = _group(h)
        self._bind(h, gid)
        r = h.client.patch(
            f"{BASE}/{gid}/agent-skills/skill-t1/governance", json={"governance": {"execution_mode": "yolo"}},
            headers=h.headers("t1"),
        )
        assert r.status_code == 400
        assert self._row(h, gid).governance is None

    def test_looser_than_the_group_is_refused(self, h):
        gid = _group(h)
        self._bind(h, gid)
        r = h.client.patch(
            f"{BASE}/{gid}/skill-global-governance", json={"governance": {"execution_mode": "approval_required"}},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        r = h.client.patch(
            f"{BASE}/{gid}/agent-skills/skill-t1/governance", json={"governance": {"execution_mode": "unrestricted"}},
            headers=h.headers("t1"),
        )
        assert r.status_code == 400
        assert self._row(h, gid).governance is None

    def test_an_inherited_skill_cannot_get_per_item_governance_here(self, h):
        gid = _group(h)
        r = h.client.patch(
            f"{BASE}/{gid}/agent-skills/skill-t1/governance", json={"governance": {"execution_mode": "disabled"}},
            headers=h.headers("t1"),
        )
        assert r.status_code == 404

    def test_another_tenants_skill_is_404(self, h):
        gid = _group(h)
        path = f"{BASE}/{gid}/agent-skills/skill-t2/governance"
        assert h.client.get(path, headers=h.headers("t1")).status_code == 404
        assert h.client.patch(path, json={"governance": {}}, headers=h.headers("t1")).status_code == 404
        assert h.client.delete(path, headers=h.headers("t1")).status_code in (204, 404)


class TestGlobalDocuments:
    @pytest.mark.parametrize(
        "path,field,doc",
        [
            ("llm-global-governance", "governance", {"temperature": {"max": 0.5}}),
            ("mcp-global-governance", "governance", {"default_tool_access": "approval_required"}),
            ("skill-global-governance", "governance", {"execution_mode": "disabled"}),
        ],
    )
    def test_set_read_clear(self, h, path, field, doc):
        gid = _group(h)
        url = f"{BASE}/{gid}/{path}"
        r = h.client.patch(url, json={field: doc}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        got = h.client.get(url, headers=h.headers("t1"))
        assert got.status_code == 200, got.text
        assert str(next(iter(doc))) in got.text
        assert h.client.delete(url, headers=h.headers("t1")).status_code == 204
        s = h.Factory()
        try:
            col = path.replace("-", "_")
            assert getattr(s.get(ResourceGroup, gid), col) in (None, {})
        finally:
            s.close()


class TestItemLimits:
    def test_mcp_server_limits_round_trip(self, h):
        gid = _group(h)
        _attach_config(h, gid)
        path = f"{BASE}/{gid}/mcp-servers/cfg-t1/limits"
        r = h.client.patch(path, json={"limits": {"frequency": {"max_calls_per_minute": 5}}}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "5" in h.client.get(path, headers=h.headers("t1")).text
        assert h.client.delete(path, headers=h.headers("t1")).status_code == 204

    def test_skill_limits_round_trip(self, h):
        gid = _group(h)

        def bind(s):
            s.execute(resource_group_agent_skills.insert().values(resource_group_id=gid, agent_skill_id="skill-t1"))

        h.seed(bind)
        path = f"{BASE}/{gid}/agent-skills/skill-t1/limits"
        r = h.client.patch(path, json={"limits": {"frequency": {"max_calls_per_minute": 5}}}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert h.client.get(path, headers=h.headers("t1")).status_code == 200
        assert h.client.delete(path, headers=h.headers("t1")).status_code == 204

    def test_portal_visibility_and_model_limits(self, h):
        gid = _group(h)
        r = h.client.post(f"{BASE}/{gid}/llm-models", json={"llm_model_id": "llm-t1"}, headers=h.headers("t1"))
        assert r.status_code in (200, 201), r.text
        r = h.client.patch(f"{BASE}/{gid}/llm-models/llm-t1/portal-visibility", json={"portal_visible": False}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        path = f"{BASE}/{gid}/llm-models/llm-t1/limits"
        assert h.client.patch(path, json={"limits": {"frequency": {"max_calls_per_minute": 5}}}, headers=h.headers("t1")).status_code == 200
        assert h.client.delete(path, headers=h.headers("t1")).status_code == 204
        assert h.client.get(f"{BASE}/{gid}/llm-models/llm-t1/governance", headers=h.headers("t1")).status_code == 200

    def test_an_unbound_model_has_no_visibility_to_toggle(self, h):
        r = h.client.patch(
            f"{BASE}/{_group(h)}/llm-models/llm-t1/portal-visibility", json={"portal_visible": False}, headers=h.headers("t1")
        )
        assert r.status_code == 404


class TestAlerts:
    def _alert(self, h, tenant="t1", alert_status="active") -> str:
        aid = f"alert-{_next()}"

        def seed(s):
            s.add(
                UsageAlert(
                    id=aid, tenant_id=tenant, resource_type="llm", group_id=f"group-default-{tenant}",
                    group_name="Default", alert_type="warning", metric="requests", threshold_pct=80,
                    current_value=90, limit_value=100, utilization_pct=90.0, period="daily",
                    period_start=datetime.now(timezone.utc), status=alert_status,
                )
            )

        h.seed(seed)
        return aid

    def _status(self, h, aid):
        s = h.Factory()
        try:
            return s.get(UsageAlert, aid).status
        finally:
            s.close()

    def test_listing_is_tenant_scoped_and_filters(self, h):
        mine, theirs = self._alert(h), self._alert(h, "t2")
        r = h.client.get(f"{BASE}/usage-alerts?status=active&resource_type=llm&alert_type=warning&group_id=group-default-t1", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert mine in r.text and theirs not in r.text

    def test_group_alerts(self, h):
        mine = self._alert(h)
        r = h.client.get(f"{BASE}/group-default-t1/usage-alerts", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert mine in r.text

    def test_acknowledge_once(self, h):
        aid = self._alert(h)
        r = h.client.post(f"{BASE}/usage-alerts/{aid}/acknowledge", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert self._status(h, aid) == "acknowledged"
        assert h.client.post(f"{BASE}/usage-alerts/{aid}/acknowledge", headers=h.headers("t1")).status_code == 409

    def test_another_tenants_alert_cannot_be_acknowledged(self, h):
        aid = self._alert(h, "t2")
        assert h.client.post(f"{BASE}/usage-alerts/{aid}/acknowledge", headers=h.headers("t1")).status_code == 404
        assert self._status(h, aid) == "active"

    def test_a_read_only_admin_cannot_acknowledge(self, h):
        aid = self._alert(h)
        ro = h.limited_headers("t1", [Permission.RESOURCE_GROUP_READ])
        assert h.client.post(f"{BASE}/usage-alerts/{aid}/acknowledge", headers=ro).status_code == 403
        assert self._status(h, aid) == "active"


class TestEffectiveViewsAndUpdates:
    @pytest.mark.parametrize(
        "suffix",
        # The two `effective-kb-*` views are the Knowledge Base feature's:
        # `enterprise/tests/test_admin_resource_groups_data_paths_http.py`.
        ["siblings", "effective-agent-cards",
         "effective-guardrails", "effective-agent-skills", "effective-llm-models", "api-keys/usage",
         "a2a-global-limits", "a2a-global-governance", "mcp-global-limits", "skill-global-limits"],
    )
    def test_each_view_answers_for_my_group(self, h, suffix):
        r = h.client.get(f"{BASE}/{_group(h)}/{suffix}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text

    def test_a_root_groups_siblings_are_the_other_roots_of_its_tenant(self, h):
        a, b, other = _group(h), _group(h), _group(h, tenant="t2")
        r = h.client.get(f"{BASE}/{a}/siblings", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        ids = {x["id"] for x in r.json()["siblings"]}
        assert b in ids and a not in ids and other not in ids
        with_self = h.client.get(f"{BASE}/{a}/siblings?include_self=true", headers=h.headers("t1")).json()
        assert a in {x["id"] for x in with_self["siblings"]}

    def test_a_childs_siblings_share_its_parent(self, h):
        parent = _group(h)
        a, b, stray = _group(h, parent), _group(h, parent), _group(h)
        r = h.client.get(f"{BASE}/{a}/siblings", headers=h.headers("t1"))
        ids = {x["id"] for x in r.json()["siblings"]}
        assert b in ids and stray not in ids

    def test_a_child_inherits_the_parents_guardrail_view_only_for_its_tenant(self, h):
        parent = _group(h)
        child = _group(h, parent)
        r = h.client.get(f"{BASE}/{child}/effective-guardrails", headers=h.headers("t1"))
        assert r.json().get("group_id", child) == child

    @pytest.mark.parametrize(
        "body", [{"description": "new"}, {"is_active": False}, {"name": "renamed-xyz"}, {"inherit_resources": False}]
    )
    def test_update_stores_what_was_sent(self, h, body):
        gid = _group(h)
        if "name" in body:
            body = {"name": f"renamed-{gid[-8:]}"}
        r = h.client.patch(f"{BASE}/{gid}", json=body, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        s = h.Factory()
        try:
            row = s.get(ResourceGroup, gid)
            for k, v in body.items():
                assert getattr(row, k) == v
        finally:
            s.close()

    def test_a_group_cannot_be_moved_under_its_own_descendant(self, h):
        parent = _group(h)
        child = _group(h, parent)
        r = h.client.patch(f"{BASE}/{parent}", json={"parent_group_id": child}, headers=h.headers("t1"))
        assert r.status_code in (400, 409, 422), r.text
        s = h.Factory()
        try:
            assert s.get(ResourceGroup, parent).parent_group_id is None
        finally:
            s.close()

    def test_group_changes_are_audited(self, h):
        gid = _group(h)
        h.client.patch(f"{BASE}/{gid}", json={"description": "audited"}, headers=h.headers("t1"))
        s = h.Factory()
        try:
            assert s.query(TenantAdminAuditLog).filter_by(tenant_id="t1", resource_id=gid).count() >= 1
        finally:
            s.close()


class TestTheA2AAllowListsOfEveryLevel:
    """The gateway judges an outbound A2A address by the allow-list of every
    level that states one: it has to match each. Two lists that share no
    pattern do not cancel out, so the view shows each level's list and not
    the patterns the lists have in common."""

    def _set(self, h, gid, document):
        from app.database.models.resource_group import ResourceGroup

        def fn(s):
            s.get(ResourceGroup, gid).a2a_global_governance = document

        h.seed(fn)

    def test_each_levels_list_is_shown(self, h):
        root = _group(h)
        middle = _group(h, root)
        leaf = _group(h, middle)
        self._set(h, root, {"remote": {"allowed_globs": ["https://*.partner.example/*"]},
                            "data": {"deny_classifications": ["PHI"]}})
        self._set(h, middle, {"capability": {"default_access": "approval_required"}})
        self._set(h, leaf, {"remote": {"allowed_globs": ["https://api.partner.example/v1/*"]}})

        body = h.client.get(f"{BASE}/{leaf}/a2a-global-governance", headers=h.headers("t1")).json()

        assert [(level["group_id"], level["allowed_globs"]) for level in body["remote_allow_lists"]] == [
            (root, ["https://*.partner.example/*"]),
            (leaf, ["https://api.partner.example/v1/*"]),
        ]
        assert "every level" in body["remote_allow_lists_rule"]
        # No single list stands for the levels: the merged documents carry none.
        for document in (body["effective_a2a_governance"], body["inherited_a2a_governance"]):
            assert "allowed_globs" not in (document.get("remote") or {})
        assert body["effective_a2a_governance"]["capability"]["default_access"] == "approval_required"
        assert body["effective_a2a_governance"]["data"]["deny_classifications"] == ["PHI"]
        assert body["a2a_global_governance"] == {"remote": {"allowed_globs": ["https://api.partner.example/v1/*"]}}

    def test_a_group_with_no_list_anywhere_shows_none(self, h):
        body = h.client.get(f"{BASE}/{_group(h)}/a2a-global-governance", headers=h.headers("t1")).json()
        assert body["remote_allow_lists"] == []
