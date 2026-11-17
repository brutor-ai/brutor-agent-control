# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The run list carries the turn count

The console's Runs table has a Turns column that reads `turn_count` and
`turn_attribution` from the list endpoint. The list never sent them, so every
row showed "—" even when the proxy had counted client-asserted turns. The list
must carry both, and an inferred count must stay distinguishable.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-turns"


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_ai_system_runs_router(BrutorLogger("run-turns-tests"))],
                            logger_name="run-turns-tests")
    now = datetime.now(timezone.utc)

    def seed(s):
        s.add(ResourceGroup(
            id=SYS, tenant_id=TENANT, name="Agent", description="a", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM,
        ))
        s.flush()
        s.add(AISystemRun(
            id="run-asserted", tenant_id=TENANT, root_task_id="root-a", ai_system_id=SYS,
            started_at=now, terminal_state="completed", finalized_by="explicit",
            is_replay=False, action_count=9, step_count=3, turn_count=3,
            turn_attribution="client_asserted",
        ))
        s.add(AISystemRun(
            id="run-inferred", tenant_id=TENANT, root_task_id="root-b", ai_system_id=SYS,
            started_at=now - timedelta(minutes=5), terminal_state="completed",
            finalized_by="idle", is_replay=False, action_count=4, step_count=0,
            turn_count=2, turn_attribution="inferred",
        ))
    h.seed(seed)
    yield h
    h.close()


def test_run_list_carries_turns_and_their_attribution(harness):
    r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/runs", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    runs = {x["id"]: x for x in r.json()["runs"]}
    assert runs["run-asserted"]["turn_count"] == 3
    assert runs["run-asserted"]["turn_attribution"] == "client_asserted"
    assert runs["run-asserted"]["step_count"] == 3
    assert runs["run-inferred"]["turn_count"] == 2
    assert runs["run-inferred"]["turn_attribution"] == "inferred"


def test_run_details_open_and_carry_turns(harness):
    # The summary carries turn_count; the details endpoint must not pass it
    # twice (every "Details" click would return a 500).
    r = harness.client.get("/v1/admin/runs/run-asserted", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["turn_count"] == 3
    assert body["turn_attribution"] == "client_asserted"

    r = harness.client.get("/v1/admin/runs/run-inferred", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    assert r.json()["turn_attribution"] == "inferred"
