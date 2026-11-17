# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Run data scope and cross-run record reach — admin API

* `GET/PUT /admin/ai-systems/{id}/data-scope` — which tool argument names which
  kind of record, and whether a call outside the run's declared scope is
  recorded (`audit`) or refused (`enforce`). The core reads it within 30 s.
* `GET /admin/ai-systems/{id}/data-scope/activity` — the last days' verdicts per
  tool, so an operator can see what `enforce` would refuse before switching it on.
* `/admin/ai-systems/{id}/reach-rules` — limits on distinct records per
  requester, key, agent or system in a window, with a preview over the
  recorded accesses so a limit is set from data rather than guessed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.ai_system_data_scope import (
    GROUP_BY_SCOPE_PREFIX,
    AISystemDataScope,
    AISystemReachRule,
    AISystemRecordAccess,
    AISystemRunScope,
    validate_config,
    validate_reach_rule,
)
from app.database.models.resource_group import GroupType, ResourceGroup

#: Rows a preview reads at most. Above it the preview says it is partial.
PREVIEW_MAX_ROWS = 200_000
PREVIEW_DAYS = 30
UNDECLARED = "(undeclared)"


class DataScopeBody(BaseModel):

    model_config = ConfigDict(extra="forbid")
    mode: str = "audit"
    on_unbound: str = "deny"
    keys: List[Dict[str, Any]] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    unscoped_tools: List[str] = Field(default_factory=list)


class DataScopeResponse(BaseModel):
    ai_system_id: str
    configured: bool
    mode: str = "off"
    on_unbound: str = "deny"
    keys: List[Dict[str, Any]] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    unscoped_tools: List[str] = Field(default_factory=list)
    version: int = 0
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class ActivityRow(BaseModel):
    tool: str
    verdict: str
    count: int


class ActivityResponse(BaseModel):
    ai_system_id: str
    days: int
    rows: List[ActivityRow]


class ReachRuleBody(BaseModel):
    scope_key: str
    group_by: str
    window_hours: int = 24
    max_distinct: int
    severity: str = "high"
    enabled: bool = True


class ReachRulePatch(BaseModel):
    """What a reach limit's update may change. A field that is not sent
    is left as it is; one that is not listed is refused."""

    model_config = ConfigDict(extra="forbid")

    scope_key: Optional[str] = None
    group_by: Optional[str] = None
    window_hours: Optional[int] = None
    max_distinct: Optional[int] = None
    severity: Optional[str] = None
    enabled: Optional[bool] = None


class ReachRuleResponse(BaseModel):
    id: str
    ai_system_id: str
    scope_key: str
    group_by: str
    window_hours: int
    max_distinct: int
    severity: str
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None


class ReachRuleList(BaseModel):
    rules: List[ReachRuleResponse]


class ReachPreviewGroup(BaseModel):
    group: str
    max_distinct_records: int


class ReachPreviewResponse(BaseModel):
    days: int
    groups_seen: int
    groups_over_limit: int
    top: List[ReachPreviewGroup]
    p95: Optional[int] = None
    p99: Optional[int] = None
    partial: bool = False


def _admin_name(admin: Any) -> Optional[str]:
    return getattr(admin, "username", None) or getattr(admin, "id", None)


def _load_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
    system = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.id == ai_system_id, ResourceGroup.tenant_id == tenant_id)
        .first()
    )
    if system is None:
        raise HTTPException(status_code=404, detail=f"AI System {ai_system_id!r} not found")
    if system.group_type != GroupType.AI_SYSTEM:
        raise HTTPException(
            status_code=422,
            detail=f"{ai_system_id!r} is not an AI System: a data scope belongs to a governed workload",
        )
    return system


def _scope_row(db: Session, tenant_id: str, ai_system_id: str) -> Optional[AISystemDataScope]:
    return (
        db.query(AISystemDataScope)
        .filter(
            AISystemDataScope.tenant_id == tenant_id,
            AISystemDataScope.ai_system_id == ai_system_id,
        )
        .first()
    )


def _percentile(sorted_values: List[int], q: float) -> Optional[int]:
    if not sorted_values:
        return None
    i = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[i]


def reach_preview(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    rule: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> ReachPreviewResponse:
    """For a draft rule: the most distinct records each group reached in any
    window ending at a day boundary (or now) over the last 30 days.

    The grouping mirrors the core's sweep (`record_reach.rs::group_expr`):
    subject, api key, agent, the whole system, or a declared scope key, with
    `(undeclared)` for a run that did not say.
    """
    now = now or datetime.now(timezone.utc)
    window = timedelta(hours=rule["window_hours"])
    start = now - timedelta(days=PREVIEW_DAYS) - window
    rows = (
        db.query(
            AISystemRecordAccess.accessed_at,
            AISystemRecordAccess.record_ref,
            AISystemRecordAccess.root_task_id,
            AISystemRecordAccess.subject_id,
            AISystemRecordAccess.api_key_id,
            AISystemRecordAccess.agent_id,
        )
        .filter(
            AISystemRecordAccess.tenant_id == tenant_id,
            AISystemRecordAccess.ai_system_id == ai_system_id,
            AISystemRecordAccess.scope_key == rule["scope_key"],
            AISystemRecordAccess.accessed_at >= start,
            AISystemRecordAccess.record_ref.isnot(None),
        )
        .order_by(AISystemRecordAccess.accessed_at.desc())
        .limit(PREVIEW_MAX_ROWS + 1)
        .all()
    )
    partial = len(rows) > PREVIEW_MAX_ROWS
    rows = rows[:PREVIEW_MAX_ROWS]

    group_by = rule["group_by"]
    scope_values: Dict[str, str] = {}
    if group_by.startswith(GROUP_BY_SCOPE_PREFIX):
        key = group_by[len(GROUP_BY_SCOPE_PREFIX):]
        roots = list({r.root_task_id for r in rows})
        for i in range(0, len(roots), 1000):
            for rs in (
                db.query(AISystemRunScope.root_task_id, AISystemRunScope.scope_values)
                .filter(
                    AISystemRunScope.tenant_id == tenant_id,
                    AISystemRunScope.scope_key == key,
                    AISystemRunScope.root_task_id.in_(roots[i : i + 1000]),
                )
                .all()
            ):
                scope_values[rs.root_task_id] = ",".join(sorted(str(v) for v in rs.scope_values or []))

    def group_of(r) -> str:
        if group_by == "subject":
            return r.subject_id or UNDECLARED
        if group_by == "api_key":
            return r.api_key_id or UNDECLARED
        if group_by == "agent":
            return r.agent_id or UNDECLARED
        if group_by == "system":
            return "system"
        return scope_values.get(r.root_task_id) or UNDECLARED

    def aware(t: datetime) -> datetime:
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ends = [day0 - timedelta(days=d) for d in range(PREVIEW_DAYS)] + [now]
    by_group: Dict[str, List[tuple]] = defaultdict(list)
    for r in rows:
        by_group[group_of(r)].append((aware(r.accessed_at), r.record_ref))
    peaks: Dict[str, int] = {}
    for grp, accesses in by_group.items():
        best = 0
        for end in ends:
            lo = end - window
            n = len({ref for at, ref in accesses if lo <= at < end})
            best = max(best, n)
        peaks[grp] = best
    ordered = sorted(peaks.values())
    top = sorted(peaks.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return ReachPreviewResponse(
        days=PREVIEW_DAYS,
        groups_seen=len(peaks),
        groups_over_limit=sum(1 for v in peaks.values() if v > rule["max_distinct"]),
        top=[ReachPreviewGroup(group=g, max_distinct_records=n) for g, n in top],
        p95=_percentile(ordered, 0.95),
        p99=_percentile(ordered, 0.99),
        partial=partial,
    )


def create_ai_system_data_scope_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Data Scope"])

    # -- data scope ------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/data-scope",
        response_model=DataScopeResponse,
        summary="Which tool arguments name which records, and how calls outside a run's scope are handled",
    )
    def get_data_scope(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> DataScopeResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        row = _scope_row(db, resolved, ai_system_id)
        if row is None:
            return DataScopeResponse(ai_system_id=ai_system_id, configured=False)
        return DataScopeResponse(configured=True, **row.to_dict())

    @router.put(
        "/admin/ai-systems/{ai_system_id}/data-scope",
        response_model=DataScopeResponse,
        summary="Set the data scope of an AI System",
        description=(
            "The core applies a change within 30 seconds. `enforce` refuses a tool "
            "call outside the run's declared scope; `audit` records it and lets it through."
        ),
    )
    def put_data_scope(
        ai_system_id: str,
        payload: DataScopeBody,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DataScopeResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        try:
            config, warnings = validate_config(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = _scope_row(db, resolved, ai_system_id)
        if row is None:
            row = AISystemDataScope(
                id=f"dsc-{ULID()}", tenant_id=resolved, ai_system_id=ai_system_id, version=0
            )
            db.add(row)
        row.mode = config["mode"]
        row.on_unbound = config["on_unbound"]
        row.keys = config["keys"]
        row.rules = config["rules"]
        row.unscoped_tools = config["unscoped_tools"]
        row.version = (row.version or 0) + 1
        row.updated_at = datetime.now(timezone.utc)
        row.updated_by = _admin_name(admin)
        db.flush()
        # A reach rule over a key that no longer exists would count nothing.
        orphaned = (
            db.query(AISystemReachRule)
            .filter(
                AISystemReachRule.tenant_id == resolved,
                AISystemReachRule.ai_system_id == ai_system_id,
                AISystemReachRule.scope_key.notin_([k["key"] for k in config["keys"]] or [""]),
            )
            .count()
        )
        if orphaned:
            warnings.append(f"{orphaned} reach rule(s) count a key this config no longer has.")
        logger.info(
            f"data scope set on {ai_system_id}: mode={row.mode} rules={len(row.rules)} v{row.version}"
        )
        return DataScopeResponse(configured=True, warnings=warnings, **row.to_dict())

    @router.get(
        "/admin/ai-systems/{ai_system_id}/data-scope/activity",
        response_model=ActivityResponse,
        summary="Verdicts per tool over recent days",
    )
    def data_scope_activity(
        ai_system_id: str,
        days: int = Query(7, ge=1, le=35),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ActivityResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = (
            db.query(
                AISystemRecordAccess.tool,
                AISystemRecordAccess.verdict,
                func.count(AISystemRecordAccess.id),
            )
            .filter(
                AISystemRecordAccess.tenant_id == resolved,
                AISystemRecordAccess.ai_system_id == ai_system_id,
                AISystemRecordAccess.accessed_at >= since,
            )
            .group_by(AISystemRecordAccess.tool, AISystemRecordAccess.verdict)
            .order_by(AISystemRecordAccess.tool, AISystemRecordAccess.verdict)
            .all()
        )
        return ActivityResponse(
            ai_system_id=ai_system_id,
            days=days,
            rows=[ActivityRow(tool=t, verdict=v, count=n) for t, v, n in rows],
        )

    # -- reach rules -----------------------------------------------------

    def _keys(db: Session, tenant_id: str, ai_system_id: str) -> List[str]:
        row = _scope_row(db, tenant_id, ai_system_id)
        return [k["key"] for k in (row.keys or [])] if row else []

    def _rule(db: Session, tenant_id: str, ai_system_id: str, rule_id: str) -> AISystemReachRule:
        rule = (
            db.query(AISystemReachRule)
            .filter(
                AISystemReachRule.id == rule_id,
                AISystemReachRule.tenant_id == tenant_id,
                AISystemReachRule.ai_system_id == ai_system_id,
            )
            .first()
        )
        if rule is None:
            raise HTTPException(status_code=404, detail=f"reach rule {rule_id!r} not found")
        return rule

    @router.get(
        "/admin/ai-systems/{ai_system_id}/reach-rules",
        response_model=ReachRuleList,
        summary="Limits on distinct records per requester across runs",
    )
    def list_reach_rules(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ReachRuleList:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        rules = (
            db.query(AISystemReachRule)
            .filter(
                AISystemReachRule.tenant_id == resolved,
                AISystemReachRule.ai_system_id == ai_system_id,
            )
            .order_by(AISystemReachRule.created_at, AISystemReachRule.id)
            .all()
        )
        return ReachRuleList(rules=[ReachRuleResponse(**r.to_dict()) for r in rules])

    @router.post(
        "/admin/ai-systems/{ai_system_id}/reach-rules",
        response_model=ReachRuleResponse,
        status_code=201,
        summary="Add a reach limit",
    )
    def create_reach_rule(
        ai_system_id: str,
        payload: ReachRuleBody,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> ReachRuleResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        try:
            clean = validate_reach_rule(payload.model_dump(), _keys(db, resolved, ai_system_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rule = AISystemReachRule(
            id=f"rrl-{ULID()}",
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            created_by=_admin_name(admin),
            **clean,
        )
        db.add(rule)
        db.flush()
        db.refresh(rule)
        return ReachRuleResponse(**rule.to_dict())

    @router.patch(
        "/admin/ai-systems/{ai_system_id}/reach-rules/{rule_id}",
        response_model=ReachRuleResponse,
        summary="Change a reach limit",
    )
    def update_reach_rule(
        ai_system_id: str,
        rule_id: str,
        body: ReachRulePatch,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> ReachRuleResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        rule = _rule(db, resolved, ai_system_id, rule_id)
        merged = {**rule.to_dict(), **body.model_dump(exclude_unset=True)}
        try:
            clean = validate_reach_rule(merged, _keys(db, resolved, ai_system_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for k, v in clean.items():
            setattr(rule, k, v)
        rule.updated_at = datetime.now(timezone.utc)
        db.flush()
        return ReachRuleResponse(**rule.to_dict())

    @router.delete(
        "/admin/ai-systems/{ai_system_id}/reach-rules/{rule_id}",
        status_code=204,
        summary="Remove a reach limit",
    )
    def delete_reach_rule(
        ai_system_id: str,
        rule_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> None:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        db.delete(_rule(db, resolved, ai_system_id, rule_id))
        db.flush()

    @router.post(
        "/admin/ai-systems/{ai_system_id}/reach-rules/preview",
        response_model=ReachPreviewResponse,
        summary="How a draft reach limit compares with the last 30 days",
        description=(
            "For each requester (or key, agent, system), the most distinct records it "
            "reached in any window ending at a day boundary or now. Read-only."
        ),
    )
    def preview_reach_rule(
        ai_system_id: str,
        payload: ReachRuleBody,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ReachPreviewResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)
        try:
            clean = validate_reach_rule(payload.model_dump(), _keys(db, resolved, ai_system_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return reach_preview(db, resolved, ai_system_id, clean)

    return router
