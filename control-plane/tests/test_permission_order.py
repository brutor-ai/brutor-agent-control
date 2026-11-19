# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The order permissions are listed in.

`GET /v1/admin/users/permissions` and the default roles list the permissions
in the order of the registry. Until the composition was turned round (L3) that
was the order of one enum, each Enterprise Edition permission standing with
the Community permissions of its kind: the registry permissions after the MCP
ones, the model-runner permissions inside `llm`. The flip registered the
Enterprise permissions after all the Community ones, so the role editor showed
seven groups at the end. The order is restored: an extension's permission says
which permission it is listed after (`PermissionSpec.after`).

`tests/fixtures/permission_order.json` is the order recorded from the
committed tree before the flip (commit 5d5fdd2). The Community order is the
same order with the Enterprise entries absent.
"""

import json
from pathlib import Path

import pytest

from app.composition.permissions import PERMISSIONS, PermissionRegistry, PermissionSpec
from app.constants.tenant import TenantDefaultRole
from app.services.tenant.tenant_provisioning import TenantProvisioner
from tests import app_factory
from tests.admin_endpoint_harness import build_admin_harness

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "permission_order.json").read_text(encoding="utf-8"))
ENTERPRISE = set(RECORDED["enterprise"])


def _expected(values):
    """The recorded list as the edition under test has it."""
    if app_factory.edition_under_test() == app_factory.ENTERPRISE:
        return list(values)
    return [value for value in values if value not in ENTERPRISE]


def test_the_recording_is_whole():
    assert len(RECORDED["order"]) == 162 and len(set(RECORDED["order"])) == 162
    assert len(ENTERPRISE) == 39 and ENTERPRISE < set(RECORDED["order"])


def test_registry_order_is_the_recorded_order():
    assert PERMISSIONS.values() == _expected(RECORDED["order"])
    assert [spec.value for spec in PERMISSIONS.specs()] == _expected(RECORDED["order"])


def test_listing_route_returns_the_recorded_order():
    harness = build_admin_harness([])
    try:
        body = harness.client.get("/v1/admin/users/permissions", headers=harness.headers()).json()
    finally:
        harness.close()
    listed = body["permissions_by_category"]
    flat = [info["name"] for infos in listed.values() for info in infos]
    expected = _expected(RECORDED["order"])
    assert sorted(flat) == sorted(expected)
    by_category = {}
    for value in expected:
        by_category.setdefault(value.split(":")[0], []).append(value)
    assert list(listed) == list(by_category)
    assert {category: [info["name"] for info in infos] for category, infos in listed.items()} == by_category
    if app_factory.edition_under_test() == app_factory.ENTERPRISE:
        assert list(listed) == RECORDED["categories"]


@pytest.mark.parametrize("role", [role.value for role in TenantDefaultRole])
def test_default_role_lists_are_in_the_recorded_order(role):
    held = TenantProvisioner(None).get_default_tenant_roles()[role]["permissions"]
    if role == TenantDefaultRole.ADMIN_USER.value and app_factory.edition_under_test() == app_factory.COMMUNITY:
        # With no system administrator the Tenant Admin holds everything but
        # `system:admin` (the recording is of an installation that has one).
        assert held == [v for v in _expected(RECORDED["order"]) if v != "system:admin"]
        return
    assert held == _expected(RECORDED["default_roles"][role])


def test_a_permission_is_listed_after_the_one_it_names():
    registry = PermissionRegistry([PermissionSpec("a:1"), PermissionSpec("b:1"), PermissionSpec("c:1")])
    registry.register([PermissionSpec("a:2", after="a:1"), PermissionSpec("a:3", after="a:2"), PermissionSpec("z:1")])
    assert registry.values() == ["a:1", "a:2", "a:3", "b:1", "c:1", "z:1"]
    assert [spec.value for spec in registry.specs()] == registry.values()


def test_a_permission_whose_anchor_is_not_registered_is_listed_last():
    registry = PermissionRegistry([PermissionSpec("a:1"), PermissionSpec("x:1", after="gone:1"), PermissionSpec("b:1")])
    assert registry.values() == ["a:1", "b:1", "x:1"]


def test_a_permission_cannot_be_listed_after_itself():
    with pytest.raises(ValueError):
        PermissionRegistry([PermissionSpec("a:1", after="a:1")])
