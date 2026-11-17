# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A role that only reads changes nothing.

Every permission says in the permission registry whether it is read-only.
The Tenant Read-Only and Tenant Auditor roles hold read-only permissions
and no others, in either edition, and a permission classified read-only
opens no route that changes something: no DELETE, PUT or PATCH, and no
POST outside the list below, which says for each why it changes nothing
(or what it changes, where that is the reader's own).
"""

from __future__ import annotations

import pytest

from app.composition.permissions import PERMISSIONS, PermissionRegistry, PermissionSpec
from app.constants.tenant import READ_ONLY_ROLES, TenantDefaultRole
from app.core.admin_user.permissions import Permission
from tests import app_factory
from tests.app_factory import create_app

# POST routes a read-only permission opens, and why holding it there changes
# nothing in the configuration or the records.
READING_POSTS = {
    # A query with a body: filters too long for a query string.
    "POST /v1/admin/proxy-logs/query": "a query",
    "POST /v1/admin/proxy-logs/facets": "a query",
    "POST /v1/admin/proxy-logs/suggest": "a query",
    "POST /v1/admin/proxy-logs/export": "builds a download of what the query lists; the export is audited",
    "POST /v1/admin/users/audit-logs/search": "a query",
    "POST /v1/admin/users/audit-logs/facets": "a query",
    "POST /v1/admin/users/audit-logs/suggest": "a query",
    "POST /v1/admin/llms/search": "a query",
    "POST /v1/admin/llms/search/with-summary": "a query",
    "POST /v1/admin/llms/user-access": "a query",
    "POST /v1/admin/video-generation-jobs/query": "a query",
    "POST /v1/admin/agent-skills/search": "a query",
    "POST /v1/admin/agent-skills/{skill_id}/export": "builds a download",
    "POST /v1/admin/batch-jobs/query": "a query",
    "POST /v1/admin/batch-jobs/{batch_job_id}/requests/query": "a query",
    "POST /v1/admin/knowledge-base/collections/{collection_id}/search": "a query",
    "POST /v1/admin/mcp-registries/search-all": "a query",
    "POST /v1/admin/mcp-registries/{registry_id}/search": "a query",
    # Computes an answer from what is stored and stores nothing.
    "POST /v1/admin/ai-systems/{ai_system_id}/liveness/learn": "proposes; accepting is another route",
    "POST /v1/admin/ai-systems/{ai_system_id}/reach-rules/preview": "a preview",
    "POST /v1/admin/ai-systems/{ai_system_id}/response-policies/preview": "a preview",
    "POST /v1/admin/assurance-digest/preview": "a preview",
    "POST /v1/admin/evidence/bundles": "builds a download from sealed records",
    "POST /v1/admin/evidence/bundles/verify": "verifies what it is given",
    "POST /v1/admin/evidence/verify": "verifies what it is given",
    "POST /v1/admin/audit/chains/{chain_id}/verify": "verifies the stored chain",
    "POST /v1/admin/end-users/{user_id}/claude-desktop-config": "builds a download",
    # Asks a system that is already configured whether it answers.
    "POST /v1/admin/mcp-servers/health/bulk": "a health probe; records the health it saw",
    "POST /v1/admin/deployments/docker/preflight": "a probe",
    "POST /v1/admin/llm-runner/machines/{machine_id}/test-connection": "a probe",
    "POST /v1/admin/usage-import/providers/{provider_id}/test": "a probe",
    "POST /v1/admin/mcp-registries/{registry_id}/test": "a probe",
    # The caller's own thirty-second ticket to open a stream it may read.
    "POST /v1/admin/system/logs/tail/ticket": "mints the caller's own single-use ticket for the log tail",
    # The reader's own saved filters of the log view, nobody else's.
    "POST /v1/admin/saved-log-views": "the caller's own saved view",
}
# Other methods a read-only permission opens.
READING_OTHER = {
    "PATCH /v1/admin/saved-log-views/{view_id}": "the caller's own saved view",
    "DELETE /v1/admin/saved-log-views/{view_id}": "the caller's own saved view",
}


def _default_roles() -> dict:
    from app.core.logging_system import BrutorLogger
    from app.services.tenant.tenant_provisioning import TenantProvisioner

    return TenantProvisioner(logger=BrutorLogger("t")).get_default_tenant_roles()


def test_every_permission_says_whether_it_is_read_only():
    for member in app_factory.every_permission():
        spec = PERMISSIONS.get(member.value)
        assert spec is not None, member.value
        assert isinstance(spec.read_only, bool)
    # The classification is stated, not derived from the name.
    assert PERMISSIONS.is_read_only(Permission.MCP_SERVER_READ.value)
    assert not PERMISSIONS.is_read_only(Permission.MCP_SERVER_UPDATE.value)
    assert not PERMISSIONS.is_read_only("no:such:permission")


@pytest.mark.parametrize("role", READ_ONLY_ROLES)
def test_a_role_that_only_reads_holds_read_only_permissions_only(role):
    held = _default_roles()[role]["permissions"]
    assert held, role
    changing = [value for value in held if not PERMISSIONS.is_read_only(value)]
    assert changing == []


def test_the_read_only_role_cannot_change_registry_connections():
    held = set(_default_roles()[TenantDefaultRole.READ_ONLY_USER.value]["permissions"])
    assert "registry:config" not in held


def test_a_changing_permission_cannot_be_granted_to_a_role_that_only_reads():
    for role in READ_ONLY_ROLES:
        with pytest.raises(ValueError, match="not read-only"):
            PermissionRegistry().register([PermissionSpec(value="ext:thing:write", default_roles=(role,))])
        PermissionRegistry().register([PermissionSpec(value="ext:thing:read", default_roles=(role,), read_only=True)])


def test_a_default_grant_of_a_changing_permission_to_a_reading_role_is_refused(monkeypatch):
    """The Community grants are written out in the provisioner: one that is
    not read-only refuses there."""
    from app.core.logging_system import BrutorLogger
    from app.services.tenant import tenant_provisioning

    original = PERMISSIONS.is_read_only
    monkeypatch.setattr(
        type(PERMISSIONS), "is_read_only",
        lambda self, value: False if value == Permission.MCP_SERVER_READ.value else original(value),
    )
    with pytest.raises(ValueError, match="only reads"):
        tenant_provisioning.TenantProvisioner(logger=BrutorLogger("t")).get_default_tenant_roles()


def _routes(app):
    def walk(routes, prefix=""):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes, prefix + route.include_context.prefix)
            elif getattr(route, "dependant", None) is not None and getattr(route, "methods", None):
                yield prefix + route.path, route

    def required(dependant, found):
        found.update(getattr(dependant.call, "required_permissions", None) or ())
        for inner in dependant.dependencies:
            required(inner, found)
        return found

    for path, route in walk(app.routes):
        permissions = required(route.dependant, set())
        for method in route.methods:
            yield f"{method} {path}", permissions


def test_a_read_only_permission_opens_no_route_that_changes_something():
    app = create_app()
    seen = 0
    wrong = []
    for operation, permissions in _routes(app):
        seen += bool(permissions)
        method = operation.split(" ")[0]
        if method in ("GET", "HEAD", "OPTIONS") or not permissions:
            continue
        # A route that asks for several permissions changes something with
        # the strictest of them.
        if any(not PERMISSIONS.is_read_only(value) for value in permissions):
            continue
        listed = READING_POSTS if method == "POST" else READING_OTHER
        if operation not in listed:
            wrong.append(f"{operation} ({', '.join(sorted(permissions))})")
    assert seen > 300, "the routes' permissions were not read"
    assert wrong == []


def test_the_role_sync_takes_a_changing_permission_from_a_reading_role():
    import uuid

    from app.database import sync_role_permissions
    from app.database.models.admin import TenantRole, TenantRolePermission
    from tests.admin_endpoint_harness import build_admin_harness

    harness = build_admin_harness([])
    try:
        s = harness.Factory()
        role = TenantRole(id="role-ro", tenant_id="t1", name=TenantDefaultRole.READ_ONLY_USER.value, is_system=True)
        custom = TenantRole(id="role-custom", tenant_id="t1", name="Operators", is_system=False)
        s.add_all([role, custom])
        s.flush()
        for owner in (role, custom):
            for value in (Permission.MCP_SERVER_UPDATE.value, Permission.MCP_SERVER_READ.value, "of:another:day"):
                s.add(TenantRolePermission(id=f"perm-{uuid.uuid4().hex[:10]}", role_id=owner.id, permission=value))
        s.commit()

        sync_role_permissions(s)

        held = {p.permission for p in s.get(TenantRole, "role-ro").permissions}
        assert Permission.MCP_SERVER_UPDATE.value not in held
        assert {Permission.MCP_SERVER_READ.value, "of:another:day"} <= held
        assert Permission.MCP_SERVER_UPDATE.value in {p.permission for p in s.get(TenantRole, "role-custom").permissions}
        s.close()
    finally:
        harness.close()
