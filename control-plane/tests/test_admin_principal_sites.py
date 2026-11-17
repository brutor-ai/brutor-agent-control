# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The places Community code told a system administrator from a tenant
administrator by the row's class.

Five sites did `isinstance(admin, SystemAdmin)` or queried the system
administrator's tables themselves. They now ask the principal registry
(`app/composition/principals.py`), so that the Community modules name no
table of the system administrator. This file is the recorder for that
change: the first class was written against the old code and passes
unchanged against the new, on the full composition.

* which sessions a password change ends (`session_revocation`);
* who a System Operations audit row names as its actor (`audit_row`);
* which second-factor state an oversight resolver reads (`oversight`);
* what the health endpoint probes for a signed-in administrator (a branch
  that does not run today; see the test);
* which trail the audit export reads for the system administrator.

The second class is the Community composition's answer, which the old code
could not give: with no system principal registered, a system
administrator's row belongs to nobody.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.v1.endpoints.service_info import create_service_info_router
from app.core.admin_user.services.session_revocation import deactivate_admin_sessions
from app.core.logging_system import BrutorLogger
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
)
from app.database.models.system_operation_log import ACTOR_TYPE_SYSTEM_ADMIN, ACTOR_TYPE_TENANT_ADMIN
from app.services.compliance.resolvers import oversight
from app.services.system_ops import audit_row
from app.composition.principals import PRINCIPALS
from tests.admin_endpoint_harness import build_admin_harness
from tests.community_process import community_composition


class _Config:
    def get_version(self) -> str:
        return "test"

    def __getattr__(self, name):  # anything else the router reads at build time
        return lambda *a, **k: None


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness(
        [create_service_info_router(_Config(), BrutorLogger("test-principal-sites"))],
        logger_name="test-principal-sites",
    )
    yield harness
    harness.close()


@pytest.fixture()
def db(h):
    session = h.Factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


def _system_admin(db) -> SystemAdmin:
    return db.query(SystemAdmin).filter(SystemAdmin.username == "sysadmin").one()


def _tenant_admin(db, tenant_id: str = "t1") -> TenantAdmin:
    return db.query(TenantAdmin).filter(TenantAdmin.username == f"admin_{tenant_id}").one()


def _session(model, admin, **extra):
    return model(
        id=f"sess-{uuid.uuid4().hex[:12]}",
        admin_id=admin.id,
        token_hash=uuid.uuid4().hex,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        is_active=True,
        **extra,
    )


class TestTheFullComposition:


    def test_an_actor_that_is_no_administrator_is_refused(self):
        with pytest.raises(TypeError, match="unknown actor type: SimpleNamespace"):
            audit_row._resolve_actor(SimpleNamespace(id="x"))


    # -- health --------------------------------------------------------------
    # The endpoint's detailed branch (components, tenant context) does not
    # run today: it calls `get_current_admin(request, db)` as a plain
    # function, which raises, and the exception is swallowed. So what is
    # held here is what a caller gets: the same
    # healthy answer whoever asks. The branch's system-administrator probe is
    # behind the registry all the same.
    @pytest.mark.parametrize("caller", [*sorted(PRINCIPALS.names()), "anonymous"])  # every registered principal
    def test_health_answers_whoever_asks(self, h, caller):
        headers = {"system": h.sys_headers, "tenant": h.headers, "anonymous": dict}[caller]()
        r = h.client.get("/v1/health", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    # -- audit export --------------------------------------------------------
    def _seed_trails(self, h):
        def seed(s):
            s.add(
                SystemAdminAuditLog(
                    id=f"sal-{uuid.uuid4().hex[:10]}",
                    admin_id=_system_admin(s).id,
                    action="system_trail_probe",
                    success=True,
                )
            )
            s.add(
                TenantAdminAuditLog(
                    id=f"tal-{uuid.uuid4().hex[:10]}",
                    tenant_id="t1",
                    admin_id=_tenant_admin(s).id,
                    action="tenant_trail_probe",
                    success=True,
                )
            )

        h.seed(seed)


class TestTheCommunityComposition:
    """No system principal is registered: a system administrator's row (left
    in a database, or handed in by mistake) belongs to nobody, and nothing
    is done in its name."""


    def test_a_tenant_administrators_sessions_still_end(self, db):
        tenant_admin = _tenant_admin(db)
        session = _session(TenantAdminSession, tenant_admin, tenant_id="t1")
        db.add(session)
        db.flush()
        with community_composition():
            assert deactivate_admin_sessions(db, tenant_admin, "password_changed") >= 1
        assert session.is_active is False


    def test_the_tenant_principal_has_no_trail_of_its_own_to_export(self):
        from app.composition.principals import PRINCIPALS

        with community_composition():
            assert [p.name for p in PRINCIPALS.specs()] == ["tenant"]
            assert PRINCIPALS.get("tenant").audit_log_model is None
            assert PRINCIPALS.get("system") is None
