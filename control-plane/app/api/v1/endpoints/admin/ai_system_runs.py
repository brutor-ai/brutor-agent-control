# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Run ledger admin API

    GET /v1/admin/ai-systems/{id}/runs        — runs for one AI System
    GET /v1/admin/ai-systems/{id}/runs/stats  — the four run signals
    GET /v1/admin/ai-systems/{id}/served-calls — calls it served in other systems' runs
    GET /v1/admin/runs/{run_id}               — one run in full
    GET /v1/admin/runs/unattributed           — the adoption funnel

Read-only. Runs are written by the Rust core's finalizer; the control
plane never mints one, which keeps a single definition of what a run is.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.services.ai_system_outcomes import RUN_CLASSES, class_cond
from app.api.v1.models.admin.ai_system_run import (
    RunDetail,
    RunListResponse,
    ServedCall,
    ServedCallListResponse,
    RunStatsResponse,
    RunTimeseriesResponse,
    RunSummary,
    UnattributedRunGroup,
    UnattributedRunsResponse,
    RunAction,
    TrajectoryResponse,
    TrajectoryStep,
    TrajectoryTurn,
    ConsequenceResponse,
    ConsequencePair,
    ActionSearchResponse,
    ActionHit,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_run import TERMINAL_STATES, AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.services.ai_system_down_rule import segment_call_outcome
from app.database.models.resource_group import ResourceGroup
from app.services.run_signals import run_signal_service
from app.database.models.session_budget_reset import SessionBudgetReset
from app.services import trajectory_search
from app.services.run_actions import project_run_actions


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _summary(run: AISystemRun, system_name: Optional[str] = None) -> RunSummary:
    return RunSummary(
        id=run.id,
        root_task_id=run.root_task_id,
        ai_system_id=run.ai_system_id,
        ai_system_name=system_name,
        resource_group_id=run.resource_group_id,
        via_system_ids=run.via_system_ids or [],
        system_kind=run.system_kind,
        actor_type=run.actor_type,
        actor_id=run.actor_id,
        subject_id=run.subject_id,
        subject_kind=run.subject_kind,
        prompt_fingerprint=run.prompt_fingerprint,
        rag_collection_ids=run.rag_collection_ids,
        outcome_id=run.outcome_id,
        outcome_conformance=run.outcome_conformance,
        outcome_counts_toward_baseline=run.outcome_counts_toward_baseline,
        outcome_contract_version=run.outcome_contract_version,
        run_class=run.run_class,
        error_cause=run.error_cause,
        client_label=run.client_label,
        agent_impl_name=run.agent_impl_name,
        agent_impl_version=run.agent_impl_version,
        agent_impl_build=run.agent_impl_build,
        agent_impl_trust=run.agent_impl_trust,
        implementation_fingerprint=run.implementation_fingerprint,
        started_at=run.started_at.isoformat() if run.started_at else None,
        ended_at=run.ended_at.isoformat() if run.ended_at else None,
        duration_ms=run.duration_ms,
        active_duration_ms=run.active_duration_ms or 0,
        awaiting_human_ms=run.awaiting_human_ms or 0,
        terminal_state=run.terminal_state,
        finalized_by=run.finalized_by,
        action_count=run.action_count or 0,
        step_count=run.step_count or 0,
        turn_count=run.turn_count or 0,
        turn_attribution=run.turn_attribution,
        llm_call_count=run.llm_call_count or 0,
        tool_call_count=run.tool_call_count or 0,
        max_delegation_depth=run.max_delegation_depth or 0,
        distinct_tools=run.distinct_tools or 0,
        tools_used=run.tools_used,
        scope_declared=run.scope_declared,
        records_touched=run.records_touched,
        scope_violation_count=run.scope_violation_count,
        scope_unbound_count=run.scope_unbound_count,
        total_tokens=run.total_tokens or 0,
        total_cost_usd=float(run.total_cost_usd or 0),
        error_count=run.error_count or 0,
        tool_error_count=run.tool_error_count or 0,
        guardrail_block_count=run.guardrail_block_count or 0,
        policy_denial_count=run.policy_denial_count or 0,
        chain_integrity=run.chain_integrity,
        source=run.source or "gateway",
        reopened_count=run.reopened_count or 0,
        is_replay=bool(run.is_replay),
    )


def _run_by_either_id(db: Session, run_id: str, tenant_id: str):
    """Look a run up by EITHER of its two identifiers.

    A run has an internal `run-<ULID>` primary key and a `root_task_id` that
    correlates its steps. Everything a user actually sees is the root: it is
    what `X-Brutor-Run-Id` returns on every LLM response, what the console's
    Chain column shows, what portal feedback is keyed on, and what the
    trajectory search puts on each action row. So the console's "Inspect"
    handed the root to endpoints that matched only on `id`, and every inspect
    404'd with the run sitting right there in the table.

    Matching both is safe: `id` is prefixed `run-` and `root_task_id` is a
    bare ULID or a derived correlation id, so the two namespaces cannot
    collide.
    """
    return (
        db.query(AISystemRun)
        .filter(
            or_(AISystemRun.id == run_id, AISystemRun.root_task_id == run_id),
            AISystemRun.tenant_id == tenant_id,
        )
        .first()
    )


def create_ai_system_runs_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Runs"])

    # ------------------------------------------------------------------
    # Session budget reset
    # ------------------------------------------------------------------
    @router.post(
        "/admin/sessions/{session_id}/budget/reset",
        summary="Reset one conversation's spend against its session budget",
        description=(
            "Clears a conversation's accumulated spend so it stops being "
            "refused by `max_cost_per_session_usd`.\n\n"
            "Ledger rows are NOT deleted — they are the audit record, and "
            "erasing spend history to satisfy a budget would destroy the "
            "evidence the spend happened. A watermark is written instead and "
            "the total is summed from it, so the conversation's history stays "
            "queryable while its budget starts again at zero.\n\n"
            "`session_id` is the derived key shown on the log row, not the "
            "chat id the client asserted."
        ),
    )
    def reset_session_budget(
        session_id: str,
        reason: Optional[str] = Query(
            None,
            max_length=500,
            description="Why the budget was cleared. Recorded for audit.",
        ),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        # A budget reset changes what the gateway will spend, so it takes
        # the write permission, not the read one the run views use.
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> dict:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        if not session_id.strip():
            raise HTTPException(status_code=422, detail="session_id is required")

        now = datetime.now(timezone.utc)
        existing = (
            db.query(SessionBudgetReset)
            .filter(
                SessionBudgetReset.tenant_id == resolved,
                SessionBudgetReset.session_id == session_id,
            )
            .first()
        )
        # A later reset supersedes an earlier one — only the most recent
        # watermark bounds the sum, so there is one row per conversation.
        if existing is not None:
            existing.reset_at = now
            existing.reset_by = getattr(admin, "username", None)
            existing.reason = reason
        else:
            db.add(
                SessionBudgetReset(
                    tenant_id=resolved,
                    session_id=session_id,
                    reset_at=now,
                    reset_by=getattr(admin, "username", None),
                    reason=reason,
                )
            )
        db.commit()

        logger.info(
            "Session budget reset | session_id=%s | tenant_id=%s | by=%s",
            session_id,
            resolved,
            getattr(admin, "username", None),
        )
        return {
            "session_id": session_id,
            "reset_at": now.isoformat(),
            "reset_by": getattr(admin, "username", None),
            "reason": reason,
            # Said explicitly so nobody reads "reset" as "deleted".
            "ledger_rows_retained": True,
        }

    # ------------------------------------------------------------------
    # Runs for one AI System
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/runs",
        response_model=RunListResponse,
        summary="List runs for an AI System",
    )
    def list_runs(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        terminal_state: Optional[str] = Query(None, description="Filter by execution state"),
        run_class: Optional[str] = Query(
            None,
            description="Filter by THE run class: success | no_work | deviation | "
            "failed | not_decisive",
        ),
        period_hours: int = Query(24, ge=0, le=8760, description="0 = all time"),
        include_replays: bool = Query(False),
        q: Optional[str] = Query(
            None,
            max_length=200,
            description=(
                "Find a run: case-insensitive substring of the run id, root task "
                "id, subject id, release name or version, or client label"
            ),
        ),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> RunListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        if terminal_state is not None and terminal_state not in TERMINAL_STATES:
            raise HTTPException(
                status_code=422,
                detail=f"terminal_state must be one of {sorted(TERMINAL_STATES)}",
            )

        query = db.query(AISystemRun).filter(
            AISystemRun.tenant_id == resolved,
            AISystemRun.ai_system_id == ai_system_id,
        )
        if terminal_state:
            query = query.filter(AISystemRun.terminal_state == terminal_state)
        if run_class is not None:
            if run_class not in RUN_CLASSES:
                raise HTTPException(
                    status_code=422, detail=f"run_class must be one of {list(RUN_CLASSES)}"
                )
            query = query.filter(class_cond(run_class))
        if period_hours:
            query = query.filter(
                AISystemRun.started_at
                >= datetime.now(timezone.utc) - timedelta(hours=period_hours)
            )
        if not include_replays:
            query = query.filter(AISystemRun.is_replay.is_(False))
        needle = (q or "").strip()
        if needle:
            # A literal substring: the operator's % and _ are characters.
            pattern = "%" + re.sub(r"([\\%_])", r"\\\1", needle.lower()) + "%"
            query = query.filter(or_(*(
                func.lower(func.coalesce(col, "")).like(pattern, escape="\\")
                for col in (
                    AISystemRun.id,
                    AISystemRun.root_task_id,
                    AISystemRun.subject_id,
                    AISystemRun.agent_impl_name,
                    AISystemRun.agent_impl_version,
                    AISystemRun.client_label,
                )
            )))

        total = query.count()
        rows = (
            query.order_by(AISystemRun.started_at.desc(), AISystemRun.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == ai_system_id,
                ResourceGroup.tenant_id == resolved,
            )
            .first()
        )
        system_name = (system.display_name or system.name) if system else None

        return RunListResponse(
            runs=[_summary(r, system_name) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------
    # Served calls
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/served-calls",
        response_model=ServedCallListResponse,
        summary="Calls an AI System served inside other systems' runs",
        description=(
            "A shared service — called over A2A or as a delegate — roots no "
            "runs of its own; its work is its segments of its callers' runs. "
            "Lists them newest first with the service's slice of each run "
            "(outcome, actions, tokens, cost, models). Open the run by "
            "`run_id` for the whole trace. Replay runs are excluded unless "
            "`include_replays`."
        ),
    )
    def list_served_calls(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(24, ge=0, le=8760, description="0 = all time"),
        include_replays: bool = Query(False),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ServedCallListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        query = (
            db.query(AISystemRunSegment)
            .join(
                AISystemRun,
                and_(
                    AISystemRun.id == AISystemRunSegment.run_id,
                    AISystemRun.tenant_id == AISystemRunSegment.tenant_id,
                ),
            )
            .filter(
                AISystemRunSegment.tenant_id == resolved,
                AISystemRunSegment.segment_system_id == ai_system_id,
                # Its own runs are on the runs list; a segment it roots is
                # not a call it served for someone else.
                or_(
                    AISystemRunSegment.root_system_id.is_(None),
                    AISystemRunSegment.root_system_id != ai_system_id,
                ),
            )
        )
        if period_hours:
            query = query.filter(
                AISystemRunSegment.first_action_at
                >= datetime.now(timezone.utc) - timedelta(hours=period_hours)
            )
        if not include_replays:
            query = query.filter(AISystemRun.is_replay.is_(False))

        total = query.count()
        rows = (
            query.order_by(AISystemRunSegment.first_action_at.desc(), AISystemRunSegment.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        # The service's own proxy rows in these runs: token split + duration.
        tasks = list({r.root_task_id for r in rows})
        split: dict = {}
        if tasks:
            for task, prompt, completion, duration in (
                db.query(
                    ProxyLog.root_task_id,
                    func.coalesce(func.sum(ProxyLog.llm_prompt_tokens), 0),
                    func.coalesce(func.sum(ProxyLog.llm_completion_tokens), 0),
                    func.sum(ProxyLog.duration_ms),
                )
                .filter(
                    ProxyLog.tenant_id == resolved,
                    ProxyLog.resource_group_id == ai_system_id,
                    ProxyLog.root_task_id.in_(tasks),
                )
                .group_by(ProxyLog.root_task_id)
                .all()
            ):
                split[task] = (int(prompt or 0), int(completion or 0), duration)

        system_ids = {i for r in rows for i in (r.root_system_id, r.caller_system_id) if i}
        names = {}
        if system_ids:
            names = {
                gid: (display or name)
                for gid, display, name in db.query(
                    ResourceGroup.id, ResourceGroup.display_name, ResourceGroup.name
                ).filter(ResourceGroup.tenant_id == resolved, ResourceGroup.id.in_(system_ids))
            }

        def _iso(v):
            return v.isoformat() if v else None

        calls = []
        for r in rows:
            prompt, completion, duration = split.get(r.root_task_id, (0, 0, None))
            models = r.models_used if isinstance(r.models_used, dict) else {}
            calls.append(ServedCall(
                segment_id=r.id,
                run_id=r.run_id,
                root_task_id=r.root_task_id,
                root_system_id=r.root_system_id,
                root_system_name=names.get(r.root_system_id),
                caller_system_id=r.caller_system_id,
                caller_system_name=names.get(r.caller_system_id),
                first_action_at=_iso(r.first_action_at),
                last_action_at=_iso(r.last_action_at),
                duration_ms=int(duration) if duration is not None else None,
                action_count=r.action_count or 0,
                llm_call_count=r.llm_call_count or 0,
                tool_call_count=r.tool_call_count or 0,
                total_tokens=int(r.total_tokens or 0),
                input_tokens=prompt,
                output_tokens=completion,
                cost_usd=float(r.cost_usd or 0),
                models=sorted(models),
                outcome=segment_call_outcome(r.outcome, r.error_cause),
                outcome_id=r.outcome_id,
                outcome_conformance=r.outcome_conformance,
                error_cause=r.error_cause,
                a2a_task_state=r.a2a_task_state,
                agent_impl_name=r.agent_impl_name,
                agent_impl_version=r.agent_impl_version,
                guardrail_block_count=r.guardrail_block_count or 0,
                policy_denial_count=r.policy_denial_count or 0,
            ))
        return ServedCallListResponse(calls=calls, total=total, limit=limit, offset=offset)

    # ------------------------------------------------------------------
    # The four run signals
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/runs/stats",
        response_model=RunStatsResponse,
        summary="Run signals for an AI System",
        description=(
            "Cost per completed run, step distribution and exhaustion, tool "
            "error rate, and the terminal-state mix. Descriptive only: "
            "nothing is compared against a baseline here."
        ),
    )
    def run_stats(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(24, ge=0, le=8760, description="0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> RunStatsResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        return run_signal_service.compute(
            db, resolved, ai_system_id=ai_system_id, period_hours=period_hours,
            start=start, end=end,
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/runs/timeseries",
        response_model=RunTimeseriesResponse,
        summary="Bucketed run signals for charts",
        description=(
            "The run signals over time: runs, outcome mix, spend, cost "
            "per completed run, tool errors and average actions per bucket. "
            "Hourly buckets at 48h and under, daily above. Sparse — quiet "
            "buckets are omitted and the client zero-fills."
        ),
    )
    def run_timeseries(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(24, ge=0, le=8760, description="0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> RunTimeseriesResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        return run_signal_service.timeseries(
            db, resolved, ai_system_id=ai_system_id, period_hours=period_hours,
            start=start, end=end,
        )

    # ------------------------------------------------------------------
    # The adoption funnel
    # ------------------------------------------------------------------

    @router.get(
        "/admin/runs/unattributed",
        response_model=UnattributedRunsResponse,
        summary="Runs not yet attributed to an AI System",
        # NOTE: declared before /admin/runs/{run_id}. FastAPI matches in
        # declaration order, so the parameter route would otherwise
        # swallow the literal "unattributed" and 404 on a run id.
        description=(
            "Traffic that has not been organised into an AI System. Not an "
            "error state — this is the list of things worth registering, "
            "grouped by the resource group they authorized against."
        ),
    )
    def unattributed_runs(
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(168, ge=0, le=8760, description="0 = all time"),
        limit: int = Query(20, ge=1, le=100, description="Groups to return"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> UnattributedRunsResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        # CP-8 — this used to `.all()` the matching runs and cluster them
        # in Python. `period_hours=0` is a documented "all time" option and
        # `limit` bounds only the *clusters returned*, never the rows
        # scanned, so on exactly the estates this screen exists for (lots
        # of traffic not yet registered as an AI System) the handler
        # materialised the entire unattributed history as ORM objects in
        # the control plane's heap — the same process that runs the
        # retention sweeper and the audit export. The aggregation is now
        # done by the database; the largest result set Python ever holds
        # is `limit` cluster rows plus five sample ids each.
        base = [
            AISystemRun.tenant_id == resolved,
            AISystemRun.ai_system_id.is_(None),
            AISystemRun.is_replay.is_(False),
        ]
        if period_hours:
            base.append(
                AISystemRun.started_at
                >= datetime.now(timezone.utc) - timedelta(hours=period_hours)
            )

        # Unattributed runs have no AI System by definition, so they are
        # clustered by actor instead — which is what the operator will use
        # to decide what each cluster actually *is*. The key is built in
        # SQL with the same 'unknown' / '-' fallbacks the Python version
        # used, so an existing cluster name does not change shape.
        actor_type = func.coalesce(AISystemRun.actor_type, "unknown")
        actor_id = func.coalesce(AISystemRun.actor_id, "-")

        cluster_rows = (
            db.query(
                actor_type.label("actor_type"),
                actor_id.label("actor_id"),
                func.count().label("runs"),
                func.coalesce(func.sum(AISystemRun.total_cost_usd), 0).label("cost"),
                func.min(AISystemRun.started_at).label("first_seen"),
                func.max(AISystemRun.started_at).label("last_seen"),
            )
            .filter(*base)
            .group_by(actor_type, actor_id)
            .order_by(func.count().desc())
            .limit(limit)
            .all()
        )

        # Totals cover EVERY cluster, not just the page — the funnel number
        # an operator reads ("N runs not yet organised") must not shrink
        # because they asked for fewer groups.
        total_runs, total_cost = db.query(
            func.count(), func.coalesce(func.sum(AISystemRun.total_cost_usd), 0)
        ).filter(*base).one()

        # Five sample root ids per returned cluster, in one query rather
        # than one per cluster: a window function ranks each cluster's runs
        # newest-first and we keep the top five. Postgres and the SQLite
        # used by the unit suite both support this.
        samples: dict = {}
        if cluster_rows:
            keys = {(r.actor_type, r.actor_id) for r in cluster_rows}
            ranked = (
                db.query(
                    actor_type.label("actor_type"),
                    actor_id.label("actor_id"),
                    AISystemRun.root_task_id.label("root_task_id"),
                    func.row_number()
                    .over(
                        partition_by=(actor_type, actor_id),
                        order_by=AISystemRun.started_at.desc(),
                    )
                    .label("rn"),
                )
                .filter(*base)
                .subquery()
            )
            for row in db.query(ranked).filter(ranked.c.rn <= 5).all():
                key = (row.actor_type, row.actor_id)
                if key in keys:
                    samples.setdefault(key, []).append(row.root_task_id)

        groups = [
            UnattributedRunGroup(
                resource_group_id=None,
                resource_group_name=f"{r.actor_type}:{r.actor_id}",
                runs=int(r.runs or 0),
                total_cost_usd=round(float(r.cost or 0), 8),
                first_seen=r.first_seen.isoformat() if r.first_seen else None,
                last_seen=r.last_seen.isoformat() if r.last_seen else None,
                sample_root_task_ids=samples.get((r.actor_type, r.actor_id), []),
            )
            for r in cluster_rows
        ]

        logger.debug(
            "Unattributed runs listed",
            context={
                "tenant_id": resolved,
                "runs": int(total_runs or 0),
                "groups": len(groups),
            },
        )

        return UnattributedRunsResponse(
            total_unattributed_runs=int(total_runs or 0),
            total_cost_usd=round(float(total_cost or 0), 8),
            groups=groups,
            period_hours=period_hours,
        )

    # ------------------------------------------------------------------
    # One run
    # ------------------------------------------------------------------

    @router.get(
        "/admin/runs/{run_id}",
        response_model=RunDetail,
        summary="Get one run",
    )
    def get_run(
        run_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> RunDetail:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        run = _run_by_either_id(db, run_id, resolved)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        system_name = None
        if run.ai_system_id:
            system = (
                db.query(ResourceGroup)
                .filter(ResourceGroup.id == run.ai_system_id)
                .first()
            )
            system_name = (system.display_name or system.name) if system else None

        # The step timeline — THE projection (`run_actions`): ordered by
        # (timestamp, id) the way the rollup's tool_signature is, capped,
        # numbered with delegations nested under the A2A call that entered
        # them, each action's outcome, target and system computed once for
        # the Runs timeline, Run details and the Composition run overlay.
        # Metadata only — bodies and headers stay in System Logs.
        actions = [RunAction(**a) for a in project_run_actions(db, resolved, run)]

        base = _summary(run, system_name).model_dump()
        # The summary carries turn_count / turn_attribution. Override them here rather than
        # passing them again below: a duplicate keyword made every run-details
        # request fail with "got multiple values for keyword argument".
        base.update(
            turn_count=run.turn_count or 0,
            turn_attribution=run.turn_attribution or "inferred",
        )
        return RunDetail(
            **base,
            actions=actions,
            agent_id=run.agent_id,
            contract_id=run.contract_id,
            contract_hash=run.contract_hash,
            trace_root_kind=run.trace_root_kind,
            skill_call_count=run.skill_call_count or 0,
            a2a_call_count=run.a2a_call_count or 0,
            tool_signature=run.tool_signature,
            tool_set_signature=run.tool_set_signature,
            prompt_tokens=run.prompt_tokens or 0,
            completion_tokens=run.completion_tokens or 0,
            models_used=run.models_used or [],
            cache_hit_count=run.cache_hit_count or 0,
            tokens_saved=run.tokens_saved or 0,
            retry_count=run.retry_count or 0,
            fallback_count=run.fallback_count or 0,
            tool_error_types=run.tool_error_types or {},
            guardrail_trip_count=run.guardrail_trip_count or 0,
            approval_request_count=run.approval_request_count or 0,
            input_bytes=run.input_bytes or 0,
            output_bytes=run.output_bytes or 0,
            first_prompt_tokens=run.first_prompt_tokens,
            peak_context_tokens=run.peak_context_tokens,
            peak_context_utilization=run.peak_context_utilization,
            context_growth=run.context_growth,
            rag_used=bool(run.rag_used),
            rag_chunks=run.rag_chunks or 0,
            feedback_score=run.feedback_score,
            created_at=run.created_at.isoformat() if run.created_at else None,
            is_successful=run.is_successful,
            is_baseline_eligible=run.is_baseline_eligible,
            is_fragmented=run.is_fragmented,
        )

    # ------------------------------------------------------------------
    # Trajectory search — Run > Step > Turn > Action
    #
    # Three read-only shapes over one denormalised table. No recursion:
    # every action row carries its own ancestry, so the hierarchy is a
    # GROUP BY and "N actions later" is a window function.
    # ------------------------------------------------------------------

    @router.get(
        "/admin/runs/{run_id}/trajectory",
        response_model=TrajectoryResponse,
        summary="Step → Turn → Action rollup for one run",
        description=(
            "Groups a run's actions by turn, optionally filtered to one "
            "asserted step. A run whose client asserted no steps is reported "
            "`unphased` rather than as a single synthetic phase."
        ),
    )
    def run_trajectory(
        run_id: str,
        step_id: Optional[str] = Query(None, description="Filter to one asserted step"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> TrajectoryResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        run = _run_by_either_id(db, run_id, resolved)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        turns = trajectory_search.turns_in_run(
            db,
            tenant_id=resolved,
            root_task_id=run.root_task_id,
            step_id=step_id,
        )
        steps = trajectory_search.cost_by_step(
            db, tenant_id=resolved, root_task_id=run.root_task_id
        )
        return TrajectoryResponse(
            run_id=run.id,
            root_task_id=run.root_task_id,
            action_count=run.action_count or 0,
            turn_count=run.turn_count or 0,
            turn_attribution=run.turn_attribution,
            step_count=run.step_count or 0,
            unphased=(run.step_count or 0) == 0,
            steps=[TrajectoryStep(**s) for s in steps],
            turns=[TrajectoryTurn(**vars(t)) for t in turns],
        )

    @router.get(
        "/admin/runs/{run_id}/consequences",
        response_model=ConsequenceResponse,
        summary="What ran shortly after a guardrail or policy trip",
        description=(
            "Co-occurrence ordered in time — NOT causation. `actions_apart` "
            "is how many actions separated the trip from the later call; the "
            "judgement stays with the operator."
        ),
    )
    def run_consequences(
        run_id: str,
        within: int = Query(10, ge=1, le=200, description="Look-ahead in actions"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ConsequenceResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        run = _run_by_either_id(db, run_id, resolved)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        rows = trajectory_search.consequences_of_trips(
            db, tenant_id=resolved, root_task_id=run.root_task_id, within=within
        )
        return ConsequenceResponse(
            run_id=run.id, within=within, pairs=[ConsequencePair(**r) for r in rows]
        )

    @router.get(
        "/admin/actions",
        response_model=ActionSearchResponse,
        summary="Find actions across runs",
        description=(
            "Fleet-wide entry point to a trajectory: filter by AI System, "
            "capability, action type, asserted step name, or blocked-only."
        ),
    )
    def search_actions(
        ai_system_id: Optional[str] = Query(None),
        capability: Optional[str] = Query(None, description="MCP/A2A capability name"),
        proxy_type: Optional[str] = Query(None, description="llm | mcp | a2a | skill"),
        step_name: Optional[str] = Query(None, description="Asserted step label"),
        only_blocked: bool = Query(False),
        period_hours: int = Query(168, ge=1, le=8760),
        limit: int = Query(200, ge=1, le=2000),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ActionSearchResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        rows = trajectory_search.find_actions(
            db,
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            capability=capability,
            proxy_type=proxy_type,
            step_name=step_name,
            only_blocked=only_blocked,
            since_hours=period_hours,
            limit=limit,
        )
        return ActionSearchResponse(
            actions=[ActionHit(**r) for r in rows], total=len(rows)
        )

    return router
