# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the query-parameter validation on the two admin
alert-listing endpoints:

  - GET /v1/admin/system-alerts   — params: status, severity, tenant_scope

Those four parameters constrain their values with a FastAPI `Query`
regex. They were migrated off FastAPI's deprecated `regex=` keyword onto
`pattern=`; the behaviour must be identical — a value matching the
constraint is accepted, a value that does not match returns 422 before
the handler runs. A typo in any of the four patterns would surface here.

The endpoints had no parameter-validation coverage before this file.

Test isolation: the auth dependencies are monkeypatched to no-ops so the
tests exercise only parameter validation, and `get_db` yields a stub
whose query chain returns no rows — so the handlers run end-to-end
without a database. (`get_metrics_alerts` does not touch the DB at all;
`list_system_alerts` issues a single `db.query(...)`.)

Run: pytest tests/test_alert_endpoints_param_validation.py -v
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging_system import BrutorLogger
from app.database import get_db


SYSTEM_ALERTS = "/v1/admin/system-alerts"


# --- stubs -----------------------------------------------------------------


class _EmptyQuery:
    """SQLAlchemy-query stand-in that matches no rows."""

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return []


class _StubDB:
    def query(self, *a, **k):
        return _EmptyQuery()


# A minimal admin object: `extract_user_context` only needs `id` +
# `username`; `list_system_alerts` only needs `tenant_id`.
_FAKE_ADMIN = types.SimpleNamespace(
    id="admin-test",
    username="tester",
    email="tester@example.com",
    tenant_id="t1",
)


def _noop_get_current_admin():
    """system_alerts.get_current_admin → (admin, admin_type_value)."""
    return (_FAKE_ADMIN, "system")


def _fake_require_permission(_permission):
    def _dep():
        return _FAKE_ADMIN

    return _dep


def _fake_require_tenant_resource_access(_resource_type):
    def _dep():
        return (_FAKE_ADMIN, "t1", "system")

    return _dep


# --- harness ---------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """TestClient wired to both alert routers with auth neutralised.

    The auth dependencies are patched in each endpoint module *before*
    the router factory runs — the routes capture `Depends(...)` targets
    at construction time, so the patch must precede `create_*_router`.
    """
    import app.api.v1.endpoints.admin.metrics as mx
    import app.api.v1.endpoints.admin.system_alerts as sa

    monkeypatch.setattr(sa, "get_current_admin", _noop_get_current_admin)
    monkeypatch.setattr(sa, "require_permission", _fake_require_permission)
    monkeypatch.setattr(mx, "require_permission", _fake_require_permission)
    monkeypatch.setattr(
        mx, "require_tenant_resource_access", _fake_require_tenant_resource_access
    )

    app = FastAPI()
    app.include_router(
        sa.create_system_alerts_router(BrutorLogger("test")), prefix="/v1"
    )
    app.include_router(
        mx.create_proxy_metrics_admin_router(types.SimpleNamespace(), BrutorLogger("test")),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = lambda: _StubDB()

    return TestClient(app)


# --- /v1/admin/system-alerts ----------------------------------------------


@pytest.mark.parametrize(
    "param,value",
    [
        ("status", "active"),
        ("status", "acknowledged"),
        ("status", "resolved"),
        ("severity", "critical"),
        ("severity", "high"),
        ("severity", "medium"),
        ("severity", "low"),
        ("tenant_scope", "self"),
        ("tenant_scope", "all"),
    ],
)
def test_system_alerts_accepts_valid_param(client, param, value):
    r = client.get(SYSTEM_ALERTS, params={param: value})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("param", ["status", "severity", "tenant_scope"])
def test_system_alerts_rejects_invalid_param(client, param):
    r = client.get(SYSTEM_ALERTS, params={param: "not-a-valid-value"})
    assert r.status_code == 422, r.text


def test_system_alerts_no_filters_ok(client):
    r = client.get(SYSTEM_ALERTS)
    assert r.status_code == 200, r.text
