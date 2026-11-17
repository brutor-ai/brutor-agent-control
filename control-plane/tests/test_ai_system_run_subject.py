# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The delegation subject on a run (a167_run_subject_rollup_excl)

The proxy stamps who an agent acted FOR on every step; the run rollup used to
drop it. The run list must now carry it, and the Tier-A vocabulary must name
it so a check can be written about delegated authority.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.constants.assurance import RUN_FACT_GUIDE, RUN_FACT_KEYS
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-agent"


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_ai_system_runs_router(BrutorLogger("run-subject-tests"))],
                            logger_name="run-subject-tests")

    def seed(s):
        s.add(ResourceGroup(
            id=SYS, tenant_id=TENANT, name="Agent", description="a", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM,
        ))
        s.flush()
        s.add(AISystemRun(
            id="run-1", tenant_id=TENANT, root_task_id="root-1", ai_system_id=SYS,
            started_at=datetime.now(timezone.utc), terminal_state="completed",
            finalized_by="explicit", is_replay=False,
            actor_type="agent_identity", actor_id="agent-7", agent_id=None,
            subject_id="42", subject_kind="user",
        ))
    h.seed(seed)
    yield h
    h.close()


def test_run_list_carries_the_subject(harness):
    r = harness.client.get(f"/v1/admin/ai-systems/{SYS}/runs", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    run = r.json()["runs"][0]
    assert run["actor_type"] == "agent_identity"
    assert run["subject_id"] == "42"
    assert run["subject_kind"] == "user"


def test_subject_is_in_the_check_vocabulary():
    assert "subject_id" in RUN_FACT_KEYS and "subject_kind" in RUN_FACT_KEYS
    assert "subject_id" in RUN_FACT_GUIDE
    assert tuple(RUN_FACT_KEYS) == tuple(sorted(RUN_FACT_KEYS)), "the pinned vocabulary stays sorted"
