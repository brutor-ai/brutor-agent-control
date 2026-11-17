# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for /v1/admin/users/audit-logs/search multi-value
filters. The trip-wire fix: `_audit_scope_query` was missing
`request_methods` and `ip_addresses` filter clauses even though both
columns were exposed as facetable. The result was the sidebar would
show value counts (computed from the facets endpoint) but clicking
a value produced no filtering — silent dead-click.

These tests seed a tenant admin's audit log with rows that vary on
request_method + ip_address + action, then assert the search endpoint
honours each filter individually and in combination.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-audit-search")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-audit-search")
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@127.0.0.1:1/x")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
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
from app.database.models.end_user import EndUser
from app.database.models.tenant import Tenant


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
]


def _create_tenant(db, tenant_id, display_name):
    t = Tenant(
        id=tenant_id, name=tenant_id, display_name=display_name,
        organization_name=display_name,
        primary_contact_email=f"admin@{tenant_id}.example",
        is_active=True, is_default=False, is_trial=False, suspended=False,
        billing_plan="self_hosted", max_admin_users=None,
    )
    db.add(t); db.flush()
    return t


def _create_tenant_admin_role(db, tenant_id):
    role = TenantRole(
        id=f"role-{tenant_id}-admin",
        tenant_id=tenant_id,
        name="Tenant Admin",
        description="Test default tenant admin",
        is_system=True,
    )
    db.add(role); db.flush()
    for p in Permission:
        if p == Permission.SYSTEM_ADMIN:
            continue
        db.add(TenantRolePermission(
            id=f"perm-{uuid.uuid4().hex[:10]}",
            role_id=role.id, permission=p.value,
        ))
    db.flush()
    return role


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
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
    app.include_router(create_admin_users_router(BrutorLogger("test")), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        _create_tenant(setup, "t1", "Tenant One")
        role = _create_tenant_admin_role(setup, "t1")
        admin = auth.create_tenant_admin(
            username="alice", email="alice@t1.example",
            password="Secret123!", tenant_id="t1",
            db=setup, role_id=role.id,
        )
        admin_id = admin.id

        # Seed: 6 rows varying on method + ip + action so each filter
        # produces a different count and we can detect crosstalk.
        seeds = [
            # (method, ip, action, success)
            ("GET",    "10.0.0.1", "mcp_server_read",   True),
            ("GET",    "10.0.0.2", "mcp_server_read",   True),
            ("POST",   "10.0.0.1", "mcp_server_create", True),
            ("PATCH",  "10.0.0.1", "mcp_server_update", True),
            ("DELETE", "10.0.0.3", "mcp_server_delete", False),
            ("POST",   "10.0.0.3", "guardrail_create",  True),
        ]
        for i, (method, ip, action, ok) in enumerate(seeds):
            setup.add(TenantAdminAuditLog(
                id=f"al-{i}",
                tenant_id="t1",
                admin_id=admin_id,
                action=action,
                resource_type="mcp_server" if "mcp" in action else "guardrail",
                resource_id=f"r-{i}",
                ip_address=ip,
                request_method=method,
                request_path=f"/v1/admin/{action}",
                success=ok,
                timestamp=datetime.now(timezone.utc),
            ))
        # One deliberately-OLD row in a distinct resource_type so the
        # 6-row baselines above are undisturbed (they pin to
        # mcp_server/guardrail). Used by TestTimeRangeFilter to prove
        # `hours_ago` actually excludes/includes by timestamp.
        setup.add(TenantAdminAuditLog(
            id="al-old",
            tenant_id="t1",
            admin_id=admin_id,
            action="ancient_event",
            resource_type="timezone_probe",
            resource_id="r-old",
            ip_address="10.0.0.9",
            request_method="GET",
            request_path="/v1/admin/ancient_event",
            success=True,
            timestamp=datetime.now(timezone.utc) - timedelta(hours=48),
        ))
        setup.commit()
    finally:
        setup.close()

    # Login + return a thin handle
    r = client.post(
        "/v1/admin/users/tenant/login",
        json={"username": "alice", "password": "Secret123!", "tenant_id": "t1"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    class H:
        pass
    h = H()
    h.client = client
    h.headers = headers
    yield h
    app.dependency_overrides.clear()
    engine.dispose()


# Pin every assertion to the seeded rows by always passing the
# resource_types filter. The login flow ALSO writes an audit row
# (action=login, resource_type=admin), which would otherwise inflate
# our totals by 1 and make the test fragile to side-effects in the
# unified-auth service.
_SEEDED_RESOURCE_TYPES = ["mcp_server", "guardrail"]


def _search(harness, **body):
    body.setdefault("resource_types", _SEEDED_RESOURCE_TYPES)
    r = harness.client.post(
        "/v1/admin/users/audit-logs/search",
        json=body,
        headers=harness.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestRequestMethodFilter:
    def test_no_filter_returns_all_seeded(self, harness):
        # Baseline — sanity check that all 6 seeded rows are visible.
        # Without this, a filter test that returns 0 rows could be
        # mistaken for the filter working.
        body = _search(harness)
        assert body["total"] == 6

    def test_single_method(self, harness):
        body = _search(harness, request_methods=["GET"])
        assert body["total"] == 2
        assert all(log["request_method"] == "GET" for log in body["logs"])

    def test_multiple_methods_or(self, harness):
        # OR-within-field: passing two methods returns rows matching
        # EITHER (3 POST/DELETE rows total — 2 POST + 1 DELETE).
        body = _search(harness, request_methods=["POST", "DELETE"])
        assert body["total"] == 3
        seen = {log["request_method"] for log in body["logs"]}
        assert seen == {"POST", "DELETE"}

    def test_unknown_method_returns_empty(self, harness):
        body = _search(harness, request_methods=["TRACE"])
        assert body["total"] == 0

    def test_empty_list_treated_as_no_filter(self, harness):
        # Pydantic Optional[List]: empty list should NOT collapse
        # the result set to 0 (an empty IN() clause would). The
        # endpoint should treat `[]` as "no filter applied".
        body = _search(harness, request_methods=[])
        assert body["total"] == 6


class TestTimeRangeFilter:
    """`hours_ago` (preset ranges from the shared TimeRangePicker) must
    filter by timestamp — previously only custom start/end did, so every
    preset (incl. the default 'last 24h') silently returned all rows."""

    # The old row lives in its own resource_type so we scope to it.
    OLD = ["timezone_probe"]

    def test_no_filter_returns_old_row(self, harness):
        body = _search(harness, resource_types=self.OLD)
        assert body["total"] == 1

    def test_hours_ago_excludes_old_row(self, harness):
        # Old row is 48h back; a 1-hour window must exclude it.
        body = _search(harness, resource_types=self.OLD, hours_ago=1)
        assert body["total"] == 0

    def test_hours_ago_includes_old_row_when_wide(self, harness):
        # A 72-hour window covers the 48h-old row.
        body = _search(harness, resource_types=self.OLD, hours_ago=72)
        assert body["total"] == 1

    def test_hours_ago_does_not_drop_recent_rows(self, harness):
        # The 6 recent rows are all "now"; a 1-hour preset keeps them.
        body = _search(harness, hours_ago=1)
        assert body["total"] == 6

    def test_start_end_still_honoured(self, harness):
        # Custom range path unchanged: a window ending 24h ago, starting
        # 72h ago, captures the 48h-old row but none of the recent ones.
        start = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        end = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        body = _search(harness, resource_types=self.OLD, start_time=start, end_time=end)
        assert body["total"] == 1


class TestIpAddressFilter:
    def test_single_ip(self, harness):
        body = _search(harness, ip_addresses=["10.0.0.1"])
        assert body["total"] == 3
        assert all(log["ip_address"] == "10.0.0.1" for log in body["logs"])

    def test_multiple_ips_or(self, harness):
        body = _search(harness, ip_addresses=["10.0.0.1", "10.0.0.3"])
        assert body["total"] == 5  # 3 from .1 + 2 from .3


class TestCombinedFilters:
    def test_method_and_ip_are_anded(self, harness):
        # AND-across-fields: GET method + .0.0.1 ip = 1 row only
        # (the second GET row is on .0.0.2).
        body = _search(
            harness,
            request_methods=["GET"],
            ip_addresses=["10.0.0.1"],
        )
        assert body["total"] == 1
        log = body["logs"][0]
        assert log["request_method"] == "GET"
        assert log["ip_address"] == "10.0.0.1"

    def test_method_and_action_and_success(self, harness):
        # Mix the new request_methods/ip_addresses with the existing
        # `actions` + `success` filters to make sure they compose.
        body = _search(
            harness,
            request_methods=["POST"],
            actions=["guardrail_create"],
            success=True,
        )
        assert body["total"] == 1
        log = body["logs"][0]
        assert log["action"] == "guardrail_create"

    def test_filter_carries_through_to_facets_endpoint(self, harness):
        # /facets must scope its counts to the active envelope — same
        # request_methods filter must reduce the per-action counts
        # accordingly. Without this, the sidebar would show stale
        # counts after clicking a method (the "haunted dropdown" bug).
        r = harness.client.post(
            "/v1/admin/users/audit-logs/facets",
            json={
                "request_methods": ["GET"],
                "resource_types": _SEEDED_RESOURCE_TYPES,
                "fields": ["action"],
            },
            headers=harness.headers,
        )
        assert r.status_code == 200, r.text
        actions = r.json()["facets"]["action"]
        # Only 'mcp_server_read' has GET rows in the seed.
        assert {a["value"] for a in actions} == {"mcp_server_read"}
        # And the count should be 2 (both GET rows are mcp_server_read).
        assert next(a["count"] for a in actions if a["value"] == "mcp_server_read") == 2
