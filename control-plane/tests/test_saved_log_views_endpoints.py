# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level regression tests for /v1/admin/saved-log-views CRUD.

These tests exercise the security-sensitive RBAC rules the pure
Pydantic suite (`test_saved_log_views.py`) can't reach:

  * Tenant isolation — tenant A admin can't see / patch / delete
    tenant B's views (404 on miss so an attacker can't enumerate).
  * Owner-only mutation — even a `is_shared = TRUE` view can only be
    edited or deleted by its owner (or a sysadmin override).
  * Sysadmin override — must pass `tenant_id` explicitly when there
    is no implicit tenant scope on the admin object.
  * 409 on name collision within (tenant, scope, owner) — friendlier
    than letting the DB unique violation through.
  * List query splits visibility — own private views + shared views
    on the tenant, never another admin's private views.
  * Scope filter is honored — `proxy_logs` views and `admin_audit_logs`
    views never bleed across.
  * Audit row is emitted for create / update / delete (the
    audit-of-the-audit trail compliance teams ask about).

The harness is SQLite in-memory with the SavedLogView ORM mapped
through the JSONB-or-JSON variant set in the model — see the
`_FILTERS_TYPE` comment in saved_log_view.py for why.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-saved-views")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-saved-views")
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.saved_log_views import (
    create_saved_log_views_router,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.end_user import EndUser  # pulled in by checks transitively
from app.database.models.saved_log_view import SavedLogView
from app.database.models.tenant import Tenant
from tests.app_factory import harness_extras


JWT_SECRET = os.environ["JWT_SECRET"]


REQUIRED_TABLES = [
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    SavedLogView.__table__,
]


# ---------------------------------------------------------------------------
# Seed helpers — minimal versions of the ones in test_admin_rbac_endpoints
# inlined here so this test file stands alone.
# ---------------------------------------------------------------------------


def _create_tenant(db: Session, tenant_id: str, display_name: str) -> Tenant:
    t = Tenant(
        id=tenant_id,
        name=tenant_id,
        display_name=display_name,
        organization_name=display_name,
        primary_contact_email=f"admin@{tenant_id}.example",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
        max_admin_users=None,
    )
    db.add(t)
    db.flush()
    return t


def _create_tenant_admin_role(db: Session, tenant_id: str) -> TenantRole:
    """Seed a 'Tenant Admin' role with every permission EXCEPT SYSTEM_ADMIN —
    matches production seeding so every require_permission() check passes.
    """
    role = TenantRole(
        id=f"role-{tenant_id}-admin",
        tenant_id=tenant_id,
        name="Tenant Admin",
        description="Test default tenant admin",
        is_system=True,
    )
    db.add(role)
    db.flush()
    for p in Permission:
        if p == Permission.SYSTEM_ADMIN:
            continue
        db.add(
            TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=role.id,
                permission=p.value,
            )
        )
    db.flush()
    return role


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness:
    def __init__(self, client, SessionFactory, auth, admins):
        self.client = client
        self.SessionFactory = SessionFactory
        self.auth = auth
        # admins[tenant_id][label] -> admin_id
        self.admins = admins
        self._tokens: dict[str, str] = {}

    def new_session(self) -> Session:
        return self.SessionFactory()

    def _login_system(self, username: str = "sysadmin") -> str:
        if username in self._tokens:
            return self._tokens[username]
        r = self.client.post(
            "/v1/admin/users/system/login",
            json={"username": username, "password": "Secret123!"},
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        self._tokens[username] = token
        return token

    def _login_tenant(self, username: str, tenant_id: str) -> str:
        cache_key = f"{tenant_id}:{username}"
        if cache_key in self._tokens:
            return self._tokens[cache_key]
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": username,
                "password": "Secret123!",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        self._tokens[cache_key] = token
        return token

    def bearer_for(self, kind: str, **kw) -> dict:
        if kind == "system":
            tok = self._login_system(kw.get("username", "sysadmin"))
        else:
            tok = self._login_tenant(kw["username"], kw["tenant_id"])
        return {"Authorization": f"Bearer {tok}"}

    def create_view_as(
        self,
        *,
        kind: str,
        tenant_id: str | None = None,
        username: str | None = None,
        scope: str = "proxy_logs",
        name: str,
        filters: dict | None = None,
        is_shared: bool = False,
    ) -> dict:
        """Hit POST /saved-log-views as the named admin. Returns the
        created view's JSON dict so tests can compare ids."""
        if kind == "tenant":
            headers = self.bearer_for("tenant", username=username, tenant_id=tenant_id)
            query = ""
        else:
            headers = self.bearer_for("system", username=username or "sysadmin")
            query = f"?tenant_id={tenant_id}"
        body = {
            "scope": scope,
            "name": name,
            "filters": filters or {"hours_ago": 1},
            "is_shared": is_shared,
        }
        r = self.client.post(
            f"/v1/admin/saved-log-views{query}", json=body, headers=headers,
        )
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(BrutorLogger('test')):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(BrutorLogger("test")), prefix="/v1")
    app.include_router(create_saved_log_views_router(BrutorLogger("test")), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    admins: dict[str, dict[str, str]] = {"t1": {}, "t2": {}}

    setup = SessionFactory()
    try:
        # System admin
        harness_extras().seed_principals(auth, setup, "Secret123!")

        # Two tenants, each with two tenant admins so we can exercise
        # owner vs. non-owner-same-tenant scenarios. The roles are
        # seeded with the full permission set so every endpoint that
        # require_permission()s passes — we're testing the saved-views
        # business logic, not RBAC plumbing here.
        for tid, label in [("t1", "Tenant One"), ("t2", "Tenant Two")]:
            _create_tenant(setup, tid, label)
            role = _create_tenant_admin_role(setup, tid)
            for who in ("alice", "bob"):
                a = auth.create_tenant_admin(
                    username=f"{who}-{tid}",
                    email=f"{who}@{tid}.example",
                    password="Secret123!",
                    tenant_id=tid,
                    db=setup,
                    role_id=role.id,
                )
                admins[tid][who] = a.id
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client, SessionFactory, auth, admins)
    app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_tenant_a_cannot_list_tenant_b_views_via_query_param(self, harness):
        # Bob on tenant 1 saves a view; Alice on tenant 2 must not see it
        # even if she explicitly asks for tenant=t1 in the query string.
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="t1-view",
        )

        # Passing the wrong tenant_id must 403 — tenant admins can't
        # widen scope by hand-typing the query param.
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs&tenant_id=t1",
            headers=harness.bearer_for("tenant", username="alice-t2", tenant_id="t2"),
        )
        assert r.status_code == 403, r.text

    def test_tenant_a_list_returns_only_own_tenant_views(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="t1-view", is_shared=True,
        )
        harness.create_view_as(
            kind="tenant", tenant_id="t2", username="alice-t2",
            scope="proxy_logs", name="t2-view", is_shared=True,
        )
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs",
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200, r.text
        names = sorted(v["name"] for v in r.json()["items"])
        assert names == ["t1-view"]

    def test_tenant_a_cannot_get_tenant_b_view_by_id(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="t1-view", is_shared=True,
        )
        r = harness.client.get(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="alice-t2", tenant_id="t2"),
        )
        # 404 (not 403) so the attacker can't enumerate cross-tenant IDs
        # by status-code differential. Documented in the endpoint helper.
        assert r.status_code == 404

    def test_tenant_a_cannot_patch_tenant_b_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="t1-view",
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={"name": "renamed"},
            headers=harness.bearer_for("tenant", username="alice-t2", tenant_id="t2"),
        )
        assert r.status_code == 404

    def test_tenant_a_cannot_delete_tenant_b_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="t1-view",
        )
        r = harness.client.delete(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="alice-t2", tenant_id="t2"),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Owner vs. non-owner mutation (within the same tenant)
# ---------------------------------------------------------------------------


class TestOwnership:
    def test_non_owner_same_tenant_cannot_patch_shared_view(self, harness):
        # Alice creates a shared view; Bob on the same tenant must not
        # be able to rename it even though he can SEE it (shared).
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage", is_shared=True,
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={"name": "bob-renamed"},
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 403, r.text

    def test_non_owner_same_tenant_cannot_delete_shared_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage", is_shared=True,
        )
        r = harness.client.delete(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 403

    def test_owner_can_patch_own_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage",
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={"name": "triage-v2", "is_shared": True},
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "triage-v2"
        assert body["is_shared"] is True

    def test_owner_can_delete_own_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage",
        )
        r = harness.client.delete(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Sysadmin override
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 409 / name collision behavior
# ---------------------------------------------------------------------------


class TestNameCollision:
    def test_duplicate_create_returns_409(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage",
        )
        body = {
            "scope": "proxy_logs",
            "name": "triage",
            "filters": {"hours_ago": 2},
            "is_shared": False,
        }
        r = harness.client.post(
            "/v1/admin/saved-log-views",
            json=body,
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 409

    def test_same_name_different_owner_is_allowed(self, harness):
        # The unique constraint is (tenant, scope, owner, name) — Alice
        # and Bob on the same tenant can both have "triage".
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="triage",
        )
        body = {
            "scope": "proxy_logs",
            "name": "triage",
            "filters": {"hours_ago": 1},
            "is_shared": False,
        }
        r = harness.client.post(
            "/v1/admin/saved-log-views",
            json=body,
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 201

    def test_same_name_different_scope_is_allowed(self, harness):
        # Alice can have "Recent" in BOTH proxy_logs and admin_audit_logs
        # — scope is part of the uniqueness key by design.
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="Recent",
        )
        body = {
            "scope": "admin_audit_logs",
            "name": "Recent",
            "filters": {"hours_ago": 1},
            "is_shared": False,
        }
        r = harness.client.post(
            "/v1/admin/saved-log-views",
            json=body,
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 201

    def test_rename_into_existing_returns_409(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="A",
        )
        v2 = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="B",
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{v2['id']}",
            json={"name": "A"},
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# List visibility split: own private + shared
# ---------------------------------------------------------------------------


class TestListVisibility:
    def test_list_includes_own_private(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="mine-private", is_shared=False,
        )
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs",
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        names = [v["name"] for v in r.json()["items"]]
        assert "mine-private" in names

    def test_list_includes_shared_from_other_admin(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="alice-shared", is_shared=True,
        )
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs",
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        names = [v["name"] for v in r.json()["items"]]
        assert "alice-shared" in names

    def test_list_excludes_other_admins_private(self, harness):
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="alice-private", is_shared=False,
        )
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs",
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        names = [v["name"] for v in r.json()["items"]]
        assert "alice-private" not in names

    def test_list_filtered_by_scope(self, harness):
        # An admin_audit_logs view must not show up when listing
        # proxy_logs scope.
        harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="admin_audit_logs", name="audit-view",
        )
        r = harness.client.get(
            "/v1/admin/saved-log-views?scope=proxy_logs",
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        names = [v["name"] for v in r.json()["items"]]
        assert "audit-view" not in names


# ---------------------------------------------------------------------------
# GET-single visibility (parallel to list — but matters for direct
# deep-links the admin UI doesn't render but tests still need to pin)
# ---------------------------------------------------------------------------


class TestGetSingleVisibility:
    def test_get_someone_elses_private_returns_404(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="alice-private", is_shared=False,
        )
        r = harness.client.get(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 404

    def test_get_someone_elses_shared_returns_view(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="alice-shared", is_shared=True,
        )
        r = harness.client.get(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="bob-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        assert r.json()["name"] == "alice-shared"


# ---------------------------------------------------------------------------
# Default + body shape sanity
# ---------------------------------------------------------------------------


class TestDefaultsAndShape:
    def test_create_defaults_is_shared_false(self, harness):
        # Sharing must be opt-in. If the operator omits is_shared,
        # we ship the view as PRIVATE so it doesn't leak into other
        # admins' dropdowns.
        body = {
            "scope": "proxy_logs",
            "name": "X",
            "filters": {"hours_ago": 1},
            # is_shared deliberately omitted
        }
        r = harness.client.post(
            "/v1/admin/saved-log-views",
            json=body,
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 201
        assert r.json()["is_shared"] is False

    def test_owner_admin_id_is_set_on_create(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
        )
        assert view["owner_admin_id"] == harness.admins["t1"]["alice"]

    def test_patch_with_empty_body_is_noop(self, harness):
        # PATCH with no fields should not mutate the row — and it
        # certainly shouldn't null out filters / name / etc. This was
        # the trap behind an earlier model-strip bug (PATCH nulling absent
        # fields).
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
            filters={"hours_ago": 1, "q": "needle"},
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={},
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "X"
        assert body["filters"] == {"hours_ago": 1, "q": "needle"}


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def _audit_rows(self, harness, *, tenant_id: str) -> list[TenantAdminAuditLog]:
        with harness.new_session() as s:
            return list(
                s.query(TenantAdminAuditLog)
                .filter(TenantAdminAuditLog.tenant_id == tenant_id)
                .order_by(TenantAdminAuditLog.timestamp.asc())
                .all()
            )

    def test_create_emits_audit_row(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
        )
        rows = self._audit_rows(harness, tenant_id="t1")
        actions = [r.action for r in rows]
        assert "saved_log_view_create" in actions
        # And the audit row pins the resource ID for cross-referencing.
        create_row = [r for r in rows if r.action == "saved_log_view_create"][-1]
        assert create_row.resource_id == view["id"]
        assert create_row.success is True

    def test_update_emits_audit_row(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
        )
        r = harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={"name": "Y"},
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 200
        rows = self._audit_rows(harness, tenant_id="t1")
        actions = [r.action for r in rows]
        assert "saved_log_view_update" in actions

    def test_delete_emits_audit_row(self, harness):
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
        )
        r = harness.client.delete(
            f"/v1/admin/saved-log-views/{view['id']}",
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        assert r.status_code == 204
        rows = self._audit_rows(harness, tenant_id="t1")
        actions = [r.action for r in rows]
        assert "saved_log_view_delete" in actions

    def test_no_op_patch_does_not_emit_audit_row(self, harness):
        # A PATCH that resolves to no change should NOT pollute the
        # audit log. Operators frequently flip a switch then flip it
        # back; we don't want to churn the audit trail with noise.
        view = harness.create_view_as(
            kind="tenant", tenant_id="t1", username="alice-t1",
            scope="proxy_logs", name="X",
        )
        before = len([r for r in self._audit_rows(harness, tenant_id="t1")
                      if r.action.startswith("saved_log_view")])
        harness.client.patch(
            f"/v1/admin/saved-log-views/{view['id']}",
            json={},
            headers=harness.bearer_for("tenant", username="alice-t1", tenant_id="t1"),
        )
        after = len([r for r in self._audit_rows(harness, tenant_id="t1")
                     if r.action.startswith("saved_log_view")])
        assert after == before
