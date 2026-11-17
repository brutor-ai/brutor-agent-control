# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Which audit table receives an administrator's action.

`UnifiedAdminAuditLogger.log` wrote to `system_admin_audit_logs` or
`tenant_admin_audit_logs` after `isinstance(admin, SystemAdmin)`. It now asks
the principal registry whose administrator it was handed, and the principal
builds its own row. The first two classes are the recording: written against
the code before that change and unchanged by it. The third is what the change
is for: the Community composition, which has no system administrator and
whose audit code names no system-administrator model.

Where a row belongs:

* an action is recorded in the trail of the principal that *acted*: a system
  administrator's in `system_admin_audit_logs`, a tenant administrator's in
  its own tenant's `tenant_admin_audit_logs`;
* a write to the global catalogs by a tenant administrator holding
  `platform:catalog:manage` (Community) is therefore in that administrator's
  tenant trail, with the action's own details saying it was global. The
  system trail's `admin_id` refers to `system_admins`, a table the Community
  Edition does not have, and the only place an installation's single
  administrator reads is its tenant's trail;
* an administrator object no registered principal owns is recorded nowhere
  (an error is logged, the caller goes on): never in some other principal's
  trail;
* with no administrator object, a type that is not registered is filed as a
  tenant-trail row, which needs an existing tenant, and never in a
  platform-wide trail.

This file is the Community Edition's part and runs for both editions: the
tenant administrator's trail, what is never written, and the Community
composition. What needs a system administrator (its own trail, its catalog
writes) is `enterprise/tests/test_admin_audit_routing.py`, which imports the
helpers here.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.constants.admin import AdminType
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.database.models.admin import (
    SystemAdminAuditLog,
    TenantAdmin,
    TenantAdminAuditLog,
)
from tests.admin_endpoint_harness import build_admin_harness
from tests.principal_matrix import community_app, community_principals

SYSTEM_COLUMNS = (
    "admin_id", "action", "resource_type", "resource_id", "affected_tenant_id", "success",
    "details", "ip_address", "user_agent", "request_method", "request_path",
)
TENANT_COLUMNS = (
    "tenant_id", "admin_id", "action", "resource_type", "resource_id", "success",
    "details", "ip_address", "user_agent", "request_method", "request_path",
)


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="test-audit-routing")
    from tests.app_factory import create_app

    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    s = harness.Factory()
    try:
        harness.tenant_admin = s.query(TenantAdmin).filter_by(username="admin_t1").one()
        harness.tenant_admin_id = harness.tenant_admin.id
        s.expunge_all()
    finally:
        s.close()
    yield harness
    harness.close()


@pytest.fixture()
def audit():
    return UnifiedAdminAuditLogger()


def _rows(h, action):
    """`(system rows, tenant rows)` of `action`, as plain dicts."""
    s = h.Factory()
    try:
        system = [
            {c: getattr(r, c) for c in SYSTEM_COLUMNS}
            for r in s.query(SystemAdminAuditLog).filter_by(action=action).all()
        ]
        tenant = [
            {c: getattr(r, c) for c in TENANT_COLUMNS}
            for r in s.query(TenantAdminAuditLog).filter_by(action=action).all()
        ]
        return system, tenant
    finally:
        s.close()


def _log(h, audit, action, **kw):
    s = h.Factory()
    try:
        return audit.log(action=action, db=s, **kw)
    finally:
        s.close()


def _action():
    return f"c7.{uuid.uuid4().hex[:10]}"


REQUEST = dict(ip_address="10.0.0.9", user_agent="pytest", request_method="POST", request_path="/v1/x")


def _entry(slug):
    return {"name": "audit routing", "slug": slug, "runtime": "remote", "is_global": True}



@pytest.fixture(scope="class")
def community(h):
    from app.database import get_db

    tokens = SimpleNamespace(
        tenant=h.headers("t1"),
        only_platform=h.limited_headers(
            "t1", [Permission.PLATFORM_CATALOG_MANAGE, Permission.MCP_CATALOG_CREATE], username="c7_audit_platform"
        ),
    )
    app = community_app(h.app.dependency_overrides[get_db])
    with community_principals():
        yield SimpleNamespace(app=app, client=TestClient(app, raise_server_exceptions=False), tokens=tokens)



class TestWhichTableReceivesTheRow:

    def test_a_tenant_administrator(self, h, audit):
        action = _action()
        audit_id = _log(
            h, audit, action, admin=h.tenant_admin, admin_type=AdminType.TENANT_ADMIN_TYPE,
            tenant_id="t1", resource_type="llm_models", resource_id="m1", details={"k": 1}, **REQUEST,
        )
        system, tenant = _rows(h, action)
        assert system == []
        assert tenant == [dict(
            tenant_id="t1", admin_id=h.tenant_admin_id, action=action, resource_type="llm_models",
            resource_id="m1", success=True, details={"k": 1}, **REQUEST,
        )]
        s = h.Factory()
        try:
            assert s.query(TenantAdminAuditLog).filter_by(id=audit_id).count() == 1
        finally:
            s.close()

    @pytest.mark.parametrize("admin_type", [AdminType.TENANT_ADMIN_TYPE, "tenant", None])
    def test_no_administrator_and_the_tenant_type_or_none(self, h, audit, admin_type):
        action = _action()
        _log(h, audit, action, admin_type=admin_type, admin_id=h.tenant_admin_id, tenant_id="t1", success=False)
        system, tenant = _rows(h, action)
        assert system == []
        assert [(r["tenant_id"], r["admin_id"], r["success"]) for r in tenant] == [("t1", h.tenant_admin_id, False)]

    @pytest.mark.parametrize("admin_type", ["robot", "System", "SYSTEM", " system", "system ", 7, "tenant_admin"])
    def test_a_type_nobody_registered_is_never_the_system_trail(self, h, audit, admin_type):
        """Filed in the tenant's trail (the narrow one: it needs an existing
        tenant), as before."""
        action = _action()
        _log(h, audit, action, admin_type=admin_type, tenant_id="t1")
        system, tenant = _rows(h, action)
        assert system == []
        assert [(r["tenant_id"], r["admin_id"]) for r in tenant] == [("t1", None)]

    @pytest.mark.parametrize("admin_type", ["robot", None, "tenant"])
    def test_a_tenant_row_without_a_tenant_is_not_written(self, h, audit, admin_type):
        action = _action()
        audit_id = _log(h, audit, action, admin_type=admin_type, admin_id=h.tenant_admin_id)
        assert isinstance(audit_id, str) and audit_id
        assert _rows(h, action) == ([], [])

    def test_a_tenant_row_for_a_tenant_that_does_not_exist_is_not_written(self, h, audit):
        action = _action()
        audit_id = _log(h, audit, action, admin_type="tenant", tenant_id="no-such-tenant", success=False)
        assert isinstance(audit_id, str) and audit_id
        assert _rows(h, action) == ([], [])


class TestWhatIsNotWritten:
    """An audit write that cannot be made does not break the caller: an id is
    returned, an error is logged, nothing is written."""

    @pytest.mark.parametrize("admin", [
        SimpleNamespace(id="x", tenant_id="t1"),
        SimpleNamespace(id="x", tenant_id=None, permissions=["system:admin"]),
        "sysadmin",
        {"id": "x"},
    ])
    def test_an_administrator_object_of_no_principal(self, h, audit, admin):
        action = _action()
        errors = []
        audit.logger = SimpleNamespace(error=lambda *a, **k: errors.append(a), warning=lambda *a, **k: None)
        audit_id = _log(h, audit, action, admin=admin, admin_type="system", tenant_id="t1")
        assert isinstance(audit_id, str) and audit_id
        assert _rows(h, action) == ([], [])
        assert len(errors) == 1

    @pytest.mark.parametrize("admin_type,tenant_id", [("tenant", "t1")])
    def test_a_write_the_database_refuses(self, h, audit, admin_type, tenant_id):
        """An `admin_id` that is no administrator of that table: the foreign
        key refuses the row. (The system trail's case is the Enterprise
        half's.)"""
        action = _action()
        errors = []
        audit.logger = SimpleNamespace(error=lambda *a, **k: errors.append(a), warning=lambda *a, **k: None)
        audit_id = _log(h, audit, action, admin_type=admin_type, admin_id="nobody", tenant_id=tenant_id)
        assert isinstance(audit_id, str) and audit_id
        assert _rows(h, action) == ([], [])
        assert len(errors) == 1

    def test_the_row_outlives_the_callers_rollback(self, h, audit):
        action = _action()
        s = h.Factory()
        try:
            audit.log(action=action, db=s, admin=h.tenant_admin, success=False)
            s.rollback()
        finally:
            s.close()
        assert len(_rows(h, action)[1]) == 1


class TestGlobalCatalogWritesByTheSystemAdministrator:

    def test_a_tenant_administrators_own_catalog_entry(self, h):
        r = h.client.post(
            "/v1/admin/mcp-catalog", json={"name": "Own", "slug": "c7-audit-own", "runtime": "remote"},
            headers=h.headers("t1"),
        )
        assert r.status_code == 201, r.text
        system, tenant = _rows(h, "mcp_catalog_create")
        assert [x for x in system if x["resource_id"] == r.json()["id"]] == []
        mine = [x for x in tenant if x["resource_id"] == r.json()["id"]]
        assert [(x["tenant_id"], x["admin_id"]) for x in mine] == [("t1", h.tenant_admin_id)]


class TestTheCommunityComposition:

    def test_a_global_llm_catalog_write_is_in_the_administrators_tenant_trail(self, community, h):
        body = {"provider_key": "openai", "model_name": "c7-audit-ce", "display_name": "C7", "mode": "chat"}
        r = community.client.post("/v1/admin/llm-catalog", json=body, headers=community.tokens.tenant)
        assert r.status_code == 201, r.text
        system, tenant = _rows(h, "llm_catalog.create")
        assert [x for x in system if x["resource_id"] == r.json()["id"]] == []
        mine = [x for x in tenant if x["resource_id"] == r.json()["id"]]
        assert [(x["tenant_id"], x["admin_id"], x["success"]) for x in mine] == [("t1", h.tenant_admin_id, True)]
        assert mine[0]["details"] == {"provider_key": "openai", "model_name": "c7-audit-ce", "mode": "chat"}

    def test_a_global_mcp_catalog_write_is_in_the_administrators_tenant_trail(self, community, h):
        c = community.client
        r = c.post("/v1/admin/mcp-catalog", json=_entry("c7-audit-ce"), headers=community.tokens.only_platform)
        assert r.status_code == 201, r.text
        entry_id = r.json()["id"]
        assert r.json()["tenant_id"] is None
        assert c.put(f"/v1/admin/mcp-catalog/{entry_id}", json={"name": "Renamed"}, headers=community.tokens.tenant).status_code == 200
        assert c.delete(f"/v1/admin/mcp-catalog/{entry_id}", headers=community.tokens.tenant).status_code in (200, 204)
        for action, said_global in (("mcp_catalog_create", None), ("mcp_catalog_update", True), ("mcp_catalog_delete", True)):
            system, tenant = _rows(h, action)
            assert [x for x in system if x["resource_id"] == entry_id] == [], action
            mine = [x for x in tenant if x["resource_id"] == entry_id]
            assert [x["tenant_id"] for x in mine] == ["t1"], action
            assert mine[0]["admin_id"] is not None
            if said_global is None:
                assert mine[0]["details"]["source"] == "system"
            else:
                assert mine[0]["details"]["is_global"] is True

    @pytest.mark.parametrize("tenant_id,expected", [(None, []), ("t1", [("t1", None)])])
    def test_the_system_type_is_not_a_platform_wide_trail(self, community, h, audit, tenant_id, expected):
        """With no administrator object the type is one nobody registered:
        filed like any other such type, in the named tenant's trail or
        nowhere."""
        action = _action()
        _log(h, audit, action, admin_type=AdminType.SYSTEM_ADMIN_TYPE, tenant_id=tenant_id, success=False)
        system, tenant = _rows(h, action)
        assert system == []
        assert [(r["tenant_id"], r["admin_id"]) for r in tenant] == expected

    def test_a_tenant_administrator_is_recorded_as_before(self, community, h, audit):
        action = _action()
        _log(h, audit, action, admin=h.tenant_admin, tenant_id="t1", details={"k": 1}, **REQUEST)
        system, tenant = _rows(h, action)
        assert system == []
        assert tenant == [dict(
            tenant_id="t1", admin_id=h.tenant_admin_id, action=action, resource_type=None,
            resource_id=None, success=True, details={"k": 1}, **REQUEST,
        )]

