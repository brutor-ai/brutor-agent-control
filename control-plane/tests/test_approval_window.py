# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a193_approval_window_epochs — the default approval window (control-plane half)

When only a tool's capability filter can name a window, every other hold
expires after 300 s. The window is a Resource Group setting (min-wins down the tree),
with a tenant default and the platform's 300 s behind it; the core resolves
every hold through one function (`services/approval_window.rs`, guarded by
`tests/approval_hold_attribution_guard.rs` there).

Pinned here:
  * the precedence rule and its bounds (pure);
  * min-wins inheritance, and a child refused a LONGER window than it
    inherits (400) — the console never stores a value the runtime discards;
  * the RG endpoints round-trip (set, read effective + source, clear);
  * the tenant default is writable, clearable and readable;
  * the contract closure carries the chain's window only when one is stated
    (a fleet with none keeps its hashes; stating one drifts the contract);
  * the platform constants match the core's (wire contract);
  * the migration's columns/constraints match the ORM (fresh installs build from it).
"""

from __future__ import annotations


import pytest

from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.api.v1.models.admin.tenant_settings import TenantSettingsUpdate
from app.constants.approval_window import (
    MAX_SECONDS,
    MIN_SECONDS,
    PLATFORM_DEFAULT_SECONDS,
    SOURCES,
    chain_window,
    effective_window,
    validate_window,
)
from app.core.logging_system import BrutorLogger
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_source


# ---------------------------------------------------------------------------
# The rule (pure)
# ---------------------------------------------------------------------------


class TestRule:
    def test_group_chain_is_min_wins_nearest_on_a_tie(self):
        assert chain_window([("team", None), ("dept", 3600), ("org", 86_400)]) == (3600, "dept")
        assert chain_window([("team", 3600), ("org", 3600)]) == (3600, "team")
        assert chain_window([("team", None), ("org", None)]) == (None, None)

    def test_group_then_tenant_then_platform(self):
        assert effective_window([("s", 86_400)], 3600) == (86_400, "resource_group", "s")
        assert effective_window([("s", None)], 3600) == (3600, "tenant", None)
        assert effective_window([("s", None)], None) == (PLATFORM_DEFAULT_SECONDS, "platform", None)

    def test_bounds(self):
        assert validate_window(None, "w") is None
        assert validate_window(MIN_SECONDS, "w") is None
        assert validate_window(MAX_SECONDS, "w") is None
        assert validate_window(MIN_SECONDS - 1, "w")
        assert validate_window(MAX_SECONDS + 1, "w")
        assert validate_window(True, "w")  # bool is not a number of seconds


class TestWireContract:
    """The core resolves; the control plane stores and explains. Both must
    agree on the default, the ceiling and the source vocabulary."""

    def test_constants_match_the_core(self):
        src = core_source("services", "approval_window.rs").read_text()
        assert f"pub const PLATFORM_DEFAULT_SECONDS: i32 = {PLATFORM_DEFAULT_SECONDS};" in src
        assert "pub const MAX_SECONDS: i32 = 7 * 24 * 3600;" in src
        assert MAX_SECONDS == 7 * 24 * 3600 == 604_800
        for s in SOURCES:
            assert f'=> "{s}"' in src, s
        assert SOURCES == ("policy", "resource_group", "tenant", "platform")

    def test_the_core_chain_walk_is_min_wins(self):
        """If the core took the nearest instead of the smallest, a parent's
        ceiling would not bind — the CP's relaxation refusal would then
        protect nothing."""
        src = core_source("services", "approval_window.rs").read_text()
        assert "MIN(approval_window_seconds) FROM chain" in src
        assert "default_approval_window_seconds FROM tenants" in src


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _group(gid, parent=None, *, window=None, level=0, tenant="t1"):
    return ResourceGroup(
        id=gid, tenant_id=tenant, name=gid, description=gid,
        hierarchy_level=level, parent_group_id=parent,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM if parent else GroupType.ORGANIZATION,
        system_kind="agent" if parent else None,
        approval_window_seconds=window,
    )


@pytest.fixture()
def h():
    log = BrutorLogger("test-approval-window")
    harness = build_admin_harness(
        [
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
            create_tenant_settings_router(log),
        ],
        logger_name="test-approval-window",
    )

    def fn(s):
        s.add(_group("org"))
        s.flush()
        s.add(_group("dept", "org", level=1))
        s.flush()
        s.add(_group("team", "dept", level=2))

    harness.seed(fn)
    yield harness
    harness.close()


def _get(h, gid):
    r = h.client.get(f"/v1/admin/resource-groups/{gid}/approval-window", headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    return r.json()


def _put(h, gid, value):
    return h.client.put(
        f"/v1/admin/resource-groups/{gid}/approval-window",
        json={"approval_window_seconds": value},
        headers=h.headers("t1"),
    )


class TestResourceGroupWindow:
    def test_unset_everywhere_is_the_platform_default(self, h):
        body = _get(h, "team")
        assert body["approval_window_seconds"] is None
        assert body["effective_seconds"] == PLATFORM_DEFAULT_SECONDS
        assert body["effective_source"] == "platform"

    def test_an_organisation_window_reaches_a_grandchild(self, h):
        """The demo case: the org says a day, the system says nothing."""
        assert _put(h, "org", 86_400).status_code == 200
        body = _get(h, "team")
        assert body["effective_seconds"] == 86_400
        assert body["effective_source"] == "resource_group"
        assert body["inherited_window_seconds"] == 86_400
        assert body["inherited_from_group_id"] == "org"

    def test_a_child_may_shorten_but_not_lengthen(self, h):
        assert _put(h, "org", 86_400).status_code == 200
        r = _put(h, "team", 172_800)
        assert r.status_code == 400, r.text
        assert "inherited" in r.json()["detail"]
        assert _get(h, "team")["approval_window_seconds"] is None
        assert _put(h, "team", 3600).status_code == 200
        body = _get(h, "team")
        assert body["approval_window_seconds"] == 3600
        assert body["effective_seconds"] == 3600

    def test_the_nearest_ancestor_is_not_the_answer_the_smallest_is(self, h):
        assert _put(h, "org", 3600).status_code == 200
        # dept may not lengthen; seed it directly to prove the runtime rule.
        h.seed(lambda s: s.query(ResourceGroup).filter(ResourceGroup.id == "dept")
               .update({"approval_window_seconds": 86_400}))
        body = _get(h, "team")
        assert body["effective_seconds"] == 3600
        assert body["inherited_from_group_id"] == "org"

    def test_out_of_range_is_refused_by_the_model(self, h):
        assert _put(h, "org", 59).status_code == 422
        assert _put(h, "org", 604_801).status_code == 422

    def test_null_clears(self, h):
        assert _put(h, "org", 86_400).status_code == 200
        assert _put(h, "org", None).status_code == 200
        assert _get(h, "org")["approval_window_seconds"] is None

    def test_other_tenants_group_is_404(self, h):
        h.seed(lambda s: s.add(_group("t2-org", tenant="t2")))
        r = h.client.get("/v1/admin/resource-groups/t2-org/approval-window", headers=h.headers("t1"))
        assert r.status_code == 404

    def test_tenant_default_applies_when_no_group_states_one(self, h):
        h.seed(lambda s: s.query(Tenant).filter(Tenant.id == "t1")
               .update({"default_approval_window_seconds": 7200}))
        body = _get(h, "team")
        assert body["tenant_default_seconds"] == 7200
        assert (body["effective_seconds"], body["effective_source"]) == (7200, "tenant")
        assert _put(h, "org", 86_400).status_code == 200
        assert _get(h, "team")["effective_source"] == "resource_group"

    def test_the_group_response_carries_the_field(self, h):
        """Pydantic strips undeclared fields: to_dict AND the response model."""
        assert _put(h, "org", 86_400).status_code == 200
        r = h.client.get("/v1/admin/resource-groups/org", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["approval_window_seconds"] == 86_400


class TestTenantDefault:

    def test_round_trip_and_explicit_null_clears(self, h):
        r = h.client.patch("/v1/admin/tenants/current", json={"default_approval_window_seconds": 86_400},
                           headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["default_approval_window_seconds"] == 86_400
        r = h.client.patch("/v1/admin/tenants/current", json={"default_approval_window_seconds": None},
                           headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["default_approval_window_seconds"] is None

    def test_out_of_range_is_refused(self, h):
        r = h.client.patch("/v1/admin/tenants/current", json={"default_approval_window_seconds": 30},
                           headers=h.headers("t1"))
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Contract closure
# ---------------------------------------------------------------------------


class TestClosure:
    def _closure(self, h, gid):
        from app.services.ai_system_closure import resolve_closure

        s = h.Factory()
        try:
            return resolve_closure(s, s.get(ResourceGroup, gid))
        finally:
            s.close()

    def test_absent_until_stated_then_the_chain_min(self, h):
        assert "approval_window_seconds" not in self._closure(h, "team")
        assert _put(h, "org", 86_400).status_code == 200
        assert self._closure(h, "team")["approval_window_seconds"] == 86_400
        assert _put(h, "team", 3600).status_code == 200
        assert self._closure(h, "team")["approval_window_seconds"] == 3600

    def test_a_tenant_default_is_not_the_systems_declaration(self, h):
        h.seed(lambda s: s.query(Tenant).filter(Tenant.id == "t1")
               .update({"default_approval_window_seconds": 7200}))
        assert "approval_window_seconds" not in self._closure(h, "team")


# ---------------------------------------------------------------------------
# a193_approval_window_epochs shape
# ---------------------------------------------------------------------------
