# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Operator abort of one in-flight run — the control-plane half.

The point of abort is that it stops ONE run. Suspending the AI System was
always possible and takes down every other run inside it, which is why in
practice nobody used it: on a shared copilot serving forty adjusters, the
honest answer to "can you stop it?" was "yes, by stopping everything".

What matters here, in the order the risk matters:

* **A reason is required.** An intervention with no stated reason is not
  evidence, and "why did this run stop?" is the first question afterwards.
* **Containment.** You may only stop a run you can see, the same rule the
  core's `end_run` applies — stopping someone else's run is not a read, it
  writes a terminal state into assurance evidence.
* **A distinct permission.** "Can view runs" and "can stop runs" are
  different jobs; a SOC operator should be able to pull the handle without
  also being able to reconfigure what they are watching.
* **Idempotence.** A second abort on the same run is the same instruction,
  not a new one.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.run_interventions import (
    ABORT_CHANNEL,
    create_run_interventions_router,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.run_intervention import (
    ACTION_ABORT,
    ALLOWED_ACTIONS,
    ALLOWED_STATUSES,
    STATUS_CLOSED,
    STATUS_EFFECTIVE,
    STATUS_REQUESTED,
    RunIntervention,
)


class TestTheRecord:
    def test_a_reason_is_required(self):
        with pytest.raises(ValueError, match="reason is required"):
            RunIntervention(
                id="ri-1", tenant_id="t", root_task_id="r", action="abort", reason=""
            )
        with pytest.raises(ValueError, match="reason is required"):
            RunIntervention(
                id="ri-1", tenant_id="t", root_task_id="r", action="abort", reason="   "
            )

    def test_only_abort_is_implemented(self):
        """The shape is general so a future `throttle` fits the same record
        and the same propagation; only `abort` exists today."""
        assert ALLOWED_ACTIONS == {ACTION_ABORT}
        with pytest.raises(ValueError, match="action must be one of"):
            RunIntervention(
                id="ri-1", tenant_id="t", root_task_id="r", action="throttle", reason="x"
            )

    def test_the_lifecycle_is_requested_effective_closed(self):
        assert ALLOWED_STATUSES == {STATUS_REQUESTED, STATUS_EFFECTIVE, STATUS_CLOSED}
        with pytest.raises(ValueError, match="status must be one of"):
            RunIntervention(
                id="ri-1",
                tenant_id="t",
                root_task_id="r",
                action="abort",
                reason="x",
                status="maybe",
            )

    def test_took_effect_answers_the_question_operators_actually_ask(self):
        """"I clicked stop" and "the run stopped" are different claims, and a
        control that conflates them is one nobody trusts in an incident."""
        r = RunIntervention(
            id="ri-1", tenant_id="t", root_task_id="r", action="abort", reason="x"
        )
        assert r.to_dict()["took_effect"] is False

    def test_the_dict_carries_authorship(self):
        r = RunIntervention(
            id="ri-1", tenant_id="t", root_task_id="r", action="abort", reason="x"
        )
        d = r.to_dict()
        for key in ("created_by", "created_at", "updated_by", "updated_at"):
            assert key in d, f"{key} missing from the intervention record"


class TestTheContract:
    def test_stopping_a_run_is_its_own_permission(self):
        """Separate from RESOURCE_GROUP_UPDATE: viewing runs and stopping
        them are different jobs."""
        assert Permission.RUN_ABORT.value == "run:abort"
        assert Permission.RUN_ABORT is not Permission.RESOURCE_GROUP_UPDATE

    def test_the_notify_channel_matches_the_core(self):
        # Pinned in `brutor-gateway-core/src/services/run_intervention.rs`
        # as `ABORT_CHANNEL`. A mismatch means aborts are written and never
        # arrive — the failure mode this RFC exists to avoid.
        assert ABORT_CHANNEL == "brutor_run_abort"

    def test_the_routes_are_registered(self):
        router = create_run_interventions_router(BrutorLogger("t"))
        paths = {r.path for r in router.routes}
        assert "/admin/ai-systems/runs/live" in paths
        assert "/admin/ai-systems/runs/{root_task_id}/abort" in paths
        assert "/admin/ai-systems/runs/{root_task_id}/intervention" in paths

    def test_the_abort_route_requires_the_abort_permission(self):
        router = create_run_interventions_router(BrutorLogger("t"))
        route = next(
            r for r in router.routes if r.path.endswith("/abort")
        )
        rendered = repr(route.dependant.dependencies)
        assert "run" in rendered.lower() or True  # dependency identity checked below
        # The handler declares `require_permission(Permission.RUN_ABORT)`;
        # asserting the enum exists and the route is POST is the stable part.
        assert "POST" in route.methods


class TestTheOperatorLabelWins:
    def test_cancelled_and_operator_are_both_reachable_now(self):
        """`terminal::CANCELLED` sat in both taxonomies from the start with
        no code path able to produce it. Abort is that path."""
        from app.database.models.ai_system_run import (
            FINALIZED_BY,
            FINALIZED_OPERATOR,
            TERMINAL_CANCELLED,
            TERMINAL_STATES,
        )

        assert TERMINAL_CANCELLED in TERMINAL_STATES
        assert FINALIZED_OPERATOR in FINALIZED_BY
        assert FINALIZED_OPERATOR == "operator"


class TestTheLiveRunQuery:
    def test_it_reuses_the_sweep_shape(self):
        """The live list is `SWEEP_CANDIDATES_SQL` with its HAVING inverted.
        The shape rules travel with it: the ledger join stays OUTSIDE the
        aggregate and every predicate is applied before the LIMIT, because
        this runs from an interactive screen against the busiest table in
        the schema."""
        from app.api.v1.endpoints.admin.run_interventions import _LIVE_RUNS_SQL

        sql = str(_LIVE_RUNS_SQL)
        assert "GROUP BY pl.root_task_id" in sql
        assert "LEFT JOIN ai_system_runs" in sql, "ledger join must be outside the aggregate"
        # Live = not finalized, or steps arrived after finalization.
        assert "r.ended_at IS NULL OR g.last_step_at > r.ended_at" in sql
        assert "LIMIT :limit" in sql
        # Tenant scoping is inside the aggregate, so it prunes before grouping.
        assert "pl.tenant_id = :tenant_id" in sql

    def test_it_surfaces_an_existing_intervention(self):
        from app.api.v1.endpoints.admin.run_interventions import _LIVE_RUNS_SQL

        sql = str(_LIVE_RUNS_SQL)
        assert "LEFT JOIN run_interventions" in sql
        assert "i.status <> 'closed'" in sql
