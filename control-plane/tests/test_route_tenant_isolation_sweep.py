# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Class guard over whole routers: every route that takes an id, called by a tenant
admin of t1 against an object owned by t2, must refuse, and an admin holding no
relevant permission must be refused everywhere. The route list is read from the
router itself, so a route added tomorrow is covered the day it lands, and a route
that forgets its tenant filter or its permission check fails here.
"""

from __future__ import annotations

import re

import pytest

from app.api.v1.endpoints.admin.agent_skills import create_agent_skills_admin_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.agent_skill import AgentSkill
from app.database.models.agent_skill_execution import AgentSkillExecution
from app.database.models.resource_group import ResourceGroup
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

log = BrutorLogger("test-isolation-sweep")
RG_ROUTER = create_resource_groups_router(UsageLimitEnforcer(logger=log), log)
# A group's effective-KB views are a router of their own on the resource-groups
# prefix, and the Knowledge Base feature's: the
# Enterprise suite sweeps it with the same four checks
# (`enterprise/tests/test_route_tenant_isolation_sweep.py`).
SKILL_ROUTER = create_agent_skills_admin_router(log)

# A permission no route in either router accepts.
UNRELATED = Permission.GUARDRAIL_TEST

NO_PERMISSION_NEEDED: set[tuple[str, str]] = set()

# Lists filtered by the caller's tenant rather than guarded by a lookup of the
# parent: another tenant's id gives an empty page, which reveals nothing. The
# seeded execution in `_seed` is what makes that assertion mean something.
FILTERED_LISTS = {("GET", "/admin/agent-skills/{skill_id}/executions")}


def _routes(router) -> list[tuple[str, str, APIRoute]]:
    out = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path, route))
    return out


def _fill(path: str, **ids) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: str(ids.get(m.group(1), "nope")), path)


def _sweep_id(method_path) -> str:
    return f"{method_path[0]} {method_path[1]}"


def harness_with(extra_routers=()):
    """The sweep's harness; `extra_routers` are mounted between the two."""
    harness = build_admin_harness([RG_ROUTER, *extra_routers, SKILL_ROUTER], logger_name="test-isolation-sweep")

    def seed(s):
        s.add(ResourceGroup(id="victim-group", tenant_id="t2", name="Victim", description="untouched",
                            hierarchy_level=0, inherit_resources=True, is_active=True))
        s.add(AgentSkill(id="victim-skill", tenant_id="t2", name="victim", description="untouched",
                         skill_md_content="# x", skill_md_hash="h"))
        s.flush()
        s.add(AgentSkillExecution(id="victim-exec", tenant_id="t2", skill_id="victim-skill",
                                  skill_name="victim", skill_version="1.0.0", execution_status="completed", request_id="req-victim"))

    harness.seed(seed)
    yield harness
    harness.close()


@pytest.fixture(scope="module")
def h():
    yield from harness_with()


def _state(h) -> tuple:
    s = h.Factory()
    try:
        g = s.get(ResourceGroup, "victim-group")
        skill = s.get(AgentSkill, "victim-skill")
        return (g.name, g.description, g.is_active, g.parent_group_id, skill.name, skill.description, skill.status)
    finally:
        s.close()


GROUP_ROUTES = [r for r in _routes(RG_ROUTER) if "{group_id}" in r[1]]
SKILL_ROUTES = [r for r in _routes(SKILL_ROUTER) if "{skill_id}" in r[1]]
ALL_ROUTES = _routes(RG_ROUTER) + _routes(SKILL_ROUTER)


def test_the_sweep_found_the_routers():
    assert len(GROUP_ROUTES) > 60
    assert len(SKILL_ROUTES) > 20


def check_a_group_of_another_tenant_is_refused(h, method, path, route):
    before = _state(h)
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(
        method, "/v1" + _fill(path, group_id="victim-group"), json={"agent_card_ids": []}, headers=h.headers("t1")
    )
    # 422 means the body was refused before the handler, which also touched nothing.
    assert r.status_code in (403, 404, 405, 422), f"{method} {path}: {r.status_code} {r.text[:200]}"
    assert "Victim" not in r.text
    assert _state(h) == before


@pytest.mark.parametrize("method,path,route", SKILL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_a_skill_of_another_tenant_is_refused(h, method, path, route):
    if (method, path) in NO_PERMISSION_NEEDED:
        pytest.skip("public by design")
    before = _state(h)
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, "/v1" + _fill(path, skill_id="victim-skill"), json={}, headers=h.headers("t1"))
    if (method, path) in FILTERED_LISTS:
        assert r.status_code == 200 and r.json()["total"] == 0
    else:
        assert r.status_code in (403, 404, 405, 422), f"{method} {path}: {r.status_code} {r.text[:200]}"
    assert "untouched" not in r.text
    assert _state(h) == before


def check_an_admin_without_a_relevant_permission_is_refused(h, method, path, route):
    if (method, path) in NO_PERMISSION_NEEDED:
        pytest.skip("public by design")
    limited = h.limited_headers("t1", [UNRELATED])
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, "/v1" + _fill(path, group_id="group-default-t1"), json={}, headers=limited)
    assert r.status_code in (403, 422), f"{method} {path}: {r.status_code} {r.text[:200]}"


def check_anonymous_is_refused(h, method, path, route):
    if (method, path) in NO_PERMISSION_NEEDED:
        pytest.skip("public by design")
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, "/v1" + _fill(path, group_id="group-default-t1"), json={})
    assert r.status_code in (401, 403, 422), f"{method} {path}: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("method,path,route", GROUP_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_a_group_of_another_tenant_is_refused(h, method, path, route):
    check_a_group_of_another_tenant_is_refused(h, method, path, route)


@pytest.mark.parametrize("method,path,route", ALL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_an_admin_without_a_relevant_permission_is_refused(h, method, path, route):
    check_an_admin_without_a_relevant_permission_is_refused(h, method, path, route)


@pytest.mark.parametrize("method,path,route", ALL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_anonymous_is_refused(h, method, path, route):
    check_anonymous_is_refused(h, method, path, route)
