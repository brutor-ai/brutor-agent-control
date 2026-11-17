# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Liveness status endpoint — HTTP behavioural spec

On the shared admin harness: real auth, full-schema SQLite.

The behaviour worth pinning is what the endpoint does with a run whose
`started_at` is in the future. That happens for real — a client with a skewed
clock, or seeded demo traffic generated across a whole calendar day when only
part of it has elapsed — and the naive subtraction returned a NEGATIVE silence,
which the admin console rendered verbatim as "Quiet for -15053s". A duration
that runs backwards is not a number an operator can act on, and a signal whose
whole job is to report absence honestly cannot be the one printing nonsense.
Zero is the honest floor: whatever else is wrong with the timestamp, a system
with a run stamped in the future is emphatically not silent.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_liveness import (
    create_ai_system_liveness_router,
)
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
FUTURE_SYS = "sys-future-clock"
PAST_SYS = "sys-past"


def _run(rid: str, system_id: str, started_at: datetime) -> AISystemRun:
    return AISystemRun(
        id=rid,
        tenant_id=TENANT,
        root_task_id=f"root-{rid}",
        ai_system_id=system_id,
        started_at=started_at,
        terminal_state="completed",
        finalized_by="explicit",
        is_replay=False,
    )


def seed(session):
    for gid, name in ((FUTURE_SYS, "Future clock"), (PAST_SYS, "Past")):
        session.add(ResourceGroup(
            id=gid, tenant_id=TENANT, name=name, description=name,
            hierarchy_level=0, inherit_resources=True, is_active=True,
            is_system=False, group_type=GroupType.AI_SYSTEM,
        ))
    session.flush()

    now = datetime.now(timezone.utc)
    # Four hours into the future — the shape the Nordica seed produced when it
    # generated a full final day at 08:42.
    session.add(_run("run-future", FUTURE_SYS, now + timedelta(hours=4)))
    session.add(_run("run-past", PAST_SYS, now - timedelta(hours=2)))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness(
        [create_ai_system_liveness_router(BrutorLogger("liveness-http-tests"))],
        logger_name="liveness-http-tests",
    )
    h.seed(seed)
    yield h
    h.close()


def _liveness(harness, system_id):
    r = harness.client.get(
        f"/v1/admin/ai-systems/{system_id}/liveness",
        headers=harness.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_a_future_run_never_reports_a_negative_silence(harness):
    body = _liveness(harness, FUTURE_SYS)
    assert body["quiet_for_seconds"] == 0
    # Clamping the number must not also clamp the fact: the last run is still
    # reported as it was stored, so the skew stays diagnosable.
    assert body["last_run_at"] is not None


def test_a_future_run_is_not_silent(harness):
    assert _liveness(harness, FUTURE_SYS)["is_silent"] is False


def test_an_ordinary_run_still_reports_its_real_silence(harness):
    quiet = _liveness(harness, PAST_SYS)["quiet_for_seconds"]
    # Two hours, allowing for the clock moving during the test.
    assert 7000 < quiet < 7400
