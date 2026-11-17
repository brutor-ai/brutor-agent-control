# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A resolver that fails is reported as "could not be evaluated" and the route
still answers 200, so a programming error in a resolver is invisible to every
test that only reads the status code (as when `oversight.py` used
`TenantAdmin` without importing it; `oversight.override_available` and
`monitoring.suspend_available` failed on every evaluation of every framework,
and the only tests that ran those lines need Postgres and are skipped in the
hermetic run).

Through the real route, for every obligation of every framework:

  * hermetic (SQLite): the only reason a statement may be unevaluable is the
    SQL dialect (`OperationalError`: the resolvers' SQL is PostgreSQL). Any
    other exception class is a defect in the resolver.
  * on Postgres (BRUTOR_PG_TESTS=1): no statement is unevaluable for an
    exception at all, and the two statements that bug broke are evaluated.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from app.api.v1.endpoints.admin.compliance_evaluation import create_compliance_evaluation_router
from app.core.logging_system import BrutorLogger
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import evaluation
from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.resolvers import ResolverContext
from app.services.compliance.resolvers.oversight import abort_holders
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

SYSTEM_ID = "sys-f410"
FAILED = re.compile(r"could not be evaluated \((\w+)\)")
#: The one exception class a resolver may raise on SQLite: its SQL is PostgreSQL.
DIALECT_ONLY = {"OperationalError"}


@pytest.fixture(scope="module")
def pg_url():
    yield from throwaway_pg_database("resolver_errors")


@pytest.fixture(scope="module")
def harness(pg_url):
    h = build_admin_harness(
        [create_compliance_evaluation_router(BrutorLogger("resolver-errors"))], database_url=pg_url
    )
    h.seed(
        lambda s: s.add(
            ResourceGroup(
                id=SYSTEM_ID, tenant_id="t1", name="f410", description="d", hierarchy_level=0,
                inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
            )
        )
    )
    evaluation.clear_cache()
    yield h
    evaluation.clear_cache()
    h.close()


def _statements(harness):
    """(framework, obligation, resolver, detail) of every statement the route
    returns, over every obligation of every framework that is offered."""
    out = []
    for fid, fw in CATALOGS.items():
        for ob in fw.obligations:
            r = harness.client.get(
                f"/v1/admin/compliance/obligations/{ob.key}", params={"framework": fid}, headers=harness.headers()
            )
            if r.status_code == 409:  # a framework that is not final law is not evaluated
                continue
            assert r.status_code == 200, (fid, ob.key, r.text)
            for system in r.json()["systems"]:
                for o in system["obligations"]:
                    for st in o["statements"]:
                        out.append((fid, ob.key, st["resolver"], st["detail"] or ""))
    return out


def test_no_resolver_fails_for_a_programming_error(harness, pg_url):
    statements = _statements(harness)
    assert len(statements) > 100, "the walk reached too few statements to mean anything"
    allowed = set() if pg_url else DIALECT_ONLY
    failures = sorted(
        {(resolver, m.group(1)) for _, _, resolver, detail in statements
         if (m := FAILED.search(detail)) and m.group(1) not in allowed}
    )
    assert not failures, f"resolvers failing with an exception (resolver, class): {failures}"


def test_the_walk_reaches_the_f410_resolvers(harness):
    resolvers = {resolver for _, _, resolver, _ in _statements(harness)}
    assert {"oversight.override_available", "monitoring.suspend_available"} <= resolvers


def test_abort_holders_counts_the_tenants_administrators(harness):
    """The line that missing import broke, on its own (it needs no PostgreSQL): the harness's
    tenant administrator of `t1` holds every permission, so `run:abort`."""
    s = harness.Factory()
    try:
        now = datetime.now(timezone.utc)
        ctx = ResolverContext(
            db=s, tenant_id="t1", system=s.get(ResourceGroup, SYSTEM_ID), window_start=now, window_end=now
        )
        assert abort_holders(ctx) == 1
        other = ResolverContext(
            db=s, tenant_id="no-such-tenant", system=s.get(ResourceGroup, SYSTEM_ID), window_start=now,
            window_end=now,
        )
        assert abort_holders(other) == 0
    finally:
        s.close()
