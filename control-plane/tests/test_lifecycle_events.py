# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A lifecycle transition is a record, not a log line. The admin who
performs it is the named approver by default; a different name is kept as a
declaration beside the recorder; the history comes back on the lifecycle
read so the console can say who moved the system and when.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-lifecycle"


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_ai_system_contracts_router(BrutorLogger("lifecycle-event-tests"))],
                            logger_name="lifecycle-event-tests")

    def seed(s):
        s.add(ResourceGroup(
            id=SYS, tenant_id=TENANT, name="Lifecycle system", description="a", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, lifecycle_stage="active",
            owner="ops@example", intended_use="Test transitions",
        ))

    h.seed(seed)
    yield h
    h.close()


def test_a_backwards_move_records_the_admin_as_approver_and_recorder(harness):
    # active → approved is always allowed; nothing typed, so the session admin
    # is both the declared approver and the recorder.
    r = harness.client.post(
        f"/v1/admin/ai-systems/{SYS}/lifecycle",
        json={"to_stage": "approved"},
        headers=harness.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to_stage"] == "approved"
    assert body["recorded_by"] == f"admin_{TENANT}"
    assert body["approved_by"] == f"admin_{TENANT}"

    s = harness.Factory()
    try:
        rows = s.query(AISystemLifecycleEvent).filter_by(ai_system_id=SYS).all()
        assert len(rows) == 1
        assert rows[0].from_stage == "active" and rows[0].to_stage == "approved"
        assert rows[0].recorded_by == f"admin_{TENANT}"
    finally:
        s.close()


def test_an_on_behalf_of_approver_is_kept_beside_the_recorder(harness):
    r = harness.client.post(
        f"/v1/admin/ai-systems/{SYS}/lifecycle",
        json={"to_stage": "proposed", "approved_by": "  Risk Owner Alice  "},
        headers=harness.headers(TENANT),
    )
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == "Risk Owner Alice"
    assert r.json()["recorded_by"] == f"admin_{TENANT}"


def test_lifecycle_read_returns_the_history_newest_first(harness):
    r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/lifecycle", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    hist = r.json()["history"]
    assert [e["to_stage"] for e in hist] == ["proposed", "approved"]
    assert hist[0]["approved_by"] == "Risk Owner Alice"
    assert hist[0]["recorded_by"] == f"admin_{TENANT}"
    assert hist[0]["at"]
    assert r.json()["current_stage"] == "proposed"
