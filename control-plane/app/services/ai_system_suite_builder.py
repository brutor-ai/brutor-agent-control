# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Freezing production traffic into a replay suite

The sampling *judgement* lives in `ai_system_replay_service` and is pure.
This is the database half: read the eligible runs, hand them to the sampler,
write the suite and its cases.

## Which runs are eligible

The same exclusions a baseline uses, and for the same reason: a case frozen
from a run the gateway only partly observed encodes a trajectory that never
happened, and every future replay would diff against fiction. So replays,
broken chains and heavily-reopened runs are all out.

## The recorded request is a retention surface

A case stores the request body as it was sent. That is what makes replay
possible at all, and it means a suite is a **copy of production data with a
different lifetime** — it outlives the run it came from by design, because
otherwise retention would silently hollow out every suite. Worth stating
plainly rather than discovering during a DPIA.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_replay import (
    AISystemReplayCase,
    AISystemReplaySuite,
    DEFAULT_SIDE_EFFECT_POLICY,
    DEFAULT_TARGET_CASE_COUNT,
)
from app.services.ai_system_replay_service import (
    RunRecord,
    coverage,
    coverage_gaps,
    select_cases,
    trajectory_of,
)

log = logging.getLogger("brutor-replay-suite-builder")

#: Reopened more than this and the run was stitched together from late steps;
#: the trajectory is not a faithful record. Matches the run ledger's cap.
MAX_REOPENS = 3

_ELIGIBLE_SQL = text(
    """
    SELECT
        r.id                AS run_id,
        r.root_task_id      AS root_task_id,
        r.terminal_state    AS terminal_state,
        r.action_count      AS action_count,
        r.tool_set_signature AS tool_set_signature,
        CAST(r.total_cost_usd AS DOUBLE PRECISION) AS cost,
        r.guardrail_block_count AS guardrail_block_count,
        r.policy_denial_count   AS policy_denial_count,
        r.approval_request_count AS approval_request_count
    FROM ai_system_runs r
    WHERE r.tenant_id = :tenant_id
      AND r.ai_system_id = :ai_system_id
      AND NOT r.is_replay
      -- Every complete observation is eligible; provenance (signed vs
      -- asserted) is disclosed on the suite, not used to exclude. See
      -- CHAIN_OBSERVED_COMPLETE in the run model.
      AND r.chain_integrity IN ('intact', 'client_asserted')
      AND r.reopened_count <= :max_reopens
      AND r.started_at >= :cutoff
    ORDER BY r.started_at ASC
    """
)

_REQUEST_SQL = text(
    """
    SELECT pl.http_method, pl.endpoint, pl.request_body_preview
      FROM proxy_logs pl
     WHERE pl.tenant_id = :tenant_id AND pl.root_task_id = :root_task_id
     ORDER BY pl.timestamp ASC, pl.id ASC
     LIMIT 1
    """
)


def eligible_runs(
    db: Session, tenant_id: str, ai_system_id: str, window_days: int = 30
) -> List[RunRecord]:
    rows = db.execute(
        _ELIGIBLE_SQL,
        {
            "tenant_id": tenant_id,
            "ai_system_id": ai_system_id,
            "max_reopens": MAX_REOPENS,
            # Computed here rather than in SQL: `MAKE_INTERVAL` is
            # Postgres-only and this query has to run on the SQLite test
            # harness too.
            "cutoff": datetime.now(timezone.utc) - timedelta(days=window_days),
        },
    ).mappings()

    return [
        RunRecord(
            run_id=r["run_id"],
            root_task_id=r["root_task_id"],
            terminal_state=r["terminal_state"],
            action_count=r["action_count"],
            tool_set_signature=r["tool_set_signature"],
            total_cost_usd=float(r["cost"] or 0.0),
            extra={
                "guardrail_block_count": r["guardrail_block_count"],
                "policy_denial_count": r["policy_denial_count"],
                "approval_request_count": r["approval_request_count"],
            },
        )
        for r in rows
    ]


def _recorded_request(db: Session, tenant_id: str, root_task_id: str) -> Dict[str, Any]:
    row = db.execute(
        _REQUEST_SQL, {"tenant_id": tenant_id, "root_task_id": root_task_id}
    ).mappings().first()
    if row is None:
        return {}

    body: Any = row["request_body_preview"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            # Kept as text rather than dropped: a body we cannot parse is
            # still evidence of what was sent, and discarding it would make
            # the case silently less faithful than it looks.
            pass

    return {"method": row["http_method"], "endpoint": row["endpoint"], "body": body}


def build_suite(
    db: Session,
    *,
    tenant_id: str,
    ai_system_id: str,
    name: str,
    description: Optional[str] = None,
    target_case_count: int = DEFAULT_TARGET_CASE_COUNT,
    window_days: int = 30,
    pass_threshold: Optional[float] = None,
    created_by: Optional[str] = None,
) -> Tuple[AISystemReplaySuite, Dict[str, Any]]:
    """Freeze a stratified sample of recent production runs.

    Returns the suite and a report of what the sample covered — and what it
    did **not**. A suite is only as good as its coverage, and "30 cases,
    none of them the exhausted path" is a fact the operator needs at build
    time, not after a green gate lets a regression through.
    """
    runs = eligible_runs(db, tenant_id, ai_system_id, window_days)
    selected = select_cases(runs, target_case_count)

    suite = AISystemReplaySuite(
        id=f"rps-{ULID()}",
        tenant_id=tenant_id,
        ai_system_id=ai_system_id,
        name=name,
        description=description,
        target_case_count=target_case_count,
        created_by=created_by,
    )
    if pass_threshold is not None:
        suite.pass_threshold = pass_threshold
    db.add(suite)
    db.flush()

    for case in selected:
        run = case.run
        trajectory = trajectory_of(run)
        db.add(
            AISystemReplayCase(
                id=f"rpc-{ULID()}",
                tenant_id=tenant_id,
                suite_id=suite.id,
                source_run_id=run.run_id,
                source_root_task_id=run.root_task_id,
                recorded_request=_recorded_request(db, tenant_id, run.root_task_id),
                recorded_trajectory=trajectory,
                recorded_terminal_state=run.terminal_state,
                recorded_action_count=run.action_count,
                recorded_tool_signature=run.tool_set_signature,
                stratum=case.stratum,
                side_effect_policy=DEFAULT_SIDE_EFFECT_POLICY,
            )
        )
    db.flush()

    report = {
        "eligible_runs": len(runs),
        "cases_created": len(selected),
        "coverage": coverage(selected),
        "coverage_gaps": coverage_gaps(runs, selected),
        "window_days": window_days,
    }
    log.info(
        "built replay suite %s for %s: %d cases from %d eligible runs",
        suite.id, ai_system_id, len(selected), len(runs),
    )
    return suite, report


__all__ = ["MAX_REOPENS", "build_suite", "eligible_runs"]
