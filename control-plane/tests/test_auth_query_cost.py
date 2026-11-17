# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The per-request authentication read model.

Every admin request used to run a tenant SELECT, a session SELECT and an
admin SELECT. The read model keeps the two yes/no answers for a few
seconds, so a burst of console requests pays for them once. Pinned here:

* a warm request issues strictly fewer statements than the cold one, and
  never touches the session table;
* logout is honoured immediately — the same token is refused on the very
  next request, inside the read-model window — because ``deactivate()``
  drops the entry;
* a tenant status edit drops that tenant's entry (ORM event), so the next
  request consults the table again.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from app.api.v1.endpoints.admin.mfa import create_me_router, create_mfa_router
from app.services import auth_read_model
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture
def harness():
    h = build_admin_harness([create_me_router(), create_mfa_router()])
    auth_read_model.clear_all()
    try:
        yield h
    finally:
        auth_read_model.clear_all()
        h.close()


class _StatementCounter:
    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        self.statements.clear()
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    @property
    def selects(self) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith("SELECT"))

    def touches(self, table: str) -> bool:
        return any(table in s for s in self.statements)


PROBE = "/v1/admin/users/me/2fa/status"


def test_tenant_status_edit_drops_the_tenant_verdict(harness):
    """The middleware asks `validate_tenant_cached`; the shared harness mounts
    no middleware, so the service is exercised directly with a real session."""
    from fastapi import HTTPException

    from app.database.models.tenant import Tenant
    from app.services.tenant.tenant_service import TenantService

    s = harness.Factory()
    try:
        tenant = s.query(Tenant).first()
        tenant_id = tenant.id

        assert TenantService.validate_tenant_cached(s, tenant_id) is not None, "cold: consults the table"
        assert TenantService.validate_tenant_cached(s, tenant_id) is None, "warm: the read model vouches"

        tenant.suspended = True
        s.commit()
        assert auth_read_model.tenant_known_active(tenant_id) is None, "the ORM event must drop it"
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant_cached(s, tenant_id)
        assert exc.value.status_code == 403, "suspended on the next request, not five seconds later"

        tenant.suspended = False
        s.commit()
        assert TenantService.validate_tenant_cached(s, tenant_id) is not None
    finally:
        s.close()
