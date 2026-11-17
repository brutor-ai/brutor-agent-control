# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Continuous Assurance Checks admin API.

The trust boundary, enforced here:

* Every deterministic spec is compile-checked by the RUST core (the one
  CEL engine) before it is stored — authoring fails closed when the core
  is unreachable.
* Spec-field changes bump the version and write an immutable snapshot;
  results and backtests always reference a version, never "whatever the
  row says now".
* Enabling a check records the enabling admin as ``approved_by`` — the
  approval act.
"""

import json
import asyncio
import functools
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.admin_user.dependencies import (
    resolve_tenant_id,
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.assurance_check import (
    CHECK_OUTPUT_BOOLEAN,
    CHECK_OUTPUT_CLASSIFICATION,
    CHECK_OUTPUTS,
    CHECK_TIER_DETERMINISTIC,
    CHECK_TIER_SEMANTIC,
    CHECK_TIERS,
    CHECK_TRIGGER_BATCH,
    CHECK_TRIGGER_ON_FINALIZE,
    THRESHOLD_KIND_RATE_OVER,
    THRESHOLD_KIND_VALUE,
    THRESHOLD_KINDS,
    THRESHOLD_OPS,
    AssuranceCheck,
    AssuranceCheckResult,
    AssuranceCheckVersion,
    compute_spec_hash,
)
from app.database.models.assurance_inbox import INBOX_SEVERITIES
from app.database.models.tenant import Tenant
from app.services.assurance_checks import core_client

# Per-tenant cap on enabled checks — a config default, deliberately NOT a
# tenant quota (the 10-quota set is closed). Overridable via
# Tenant.settings["assurance_checks"]["max_enabled"].
DEFAULT_MAX_ENABLED_CHECKS = 25


class ThresholdInput(BaseModel):
    kind: str = THRESHOLD_KIND_VALUE
    op: str = "eq"
    value: Any = True
    severity: str = "warning"
    window_hours: Optional[int] = Field(None, ge=1, le=168)


class CheckCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description_nl: Optional[str] = None
    scope: Dict[str, Any] = Field(default_factory=dict)
    tier: str
    evaluator: Dict[str, Any]
    output: str = CHECK_OUTPUT_BOOLEAN
    trigger: Optional[str] = None
    threshold: Optional[ThresholdInput] = None
    route_to_inbox: bool = True


class CheckUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description_nl: Optional[str] = None
    scope: Optional[Dict[str, Any]] = None
    evaluator: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    threshold: Optional[ThresholdInput] = None
    route_to_inbox: Optional[bool] = None
    enabled: Optional[bool] = None


class CheckResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description_nl: Optional[str] = None
    version: int
    spec_hash: str
    scope: Dict[str, Any]
    tier: str
    evaluator: Dict[str, Any]
    output: str
    trigger: str
    threshold: Optional[Dict[str, Any]] = None
    route_to_inbox: bool
    enabled: bool
    created_by: Optional[str] = None
    approved_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CheckVersionResponse(BaseModel):
    id: str
    check_id: str
    version: int
    spec_hash: str
    spec: Dict[str, Any]
    backtest_summary: Optional[Dict[str, Any]] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class CheckResultResponse(BaseModel):
    id: str
    check_id: str
    check_version: int
    run_id: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    value: Dict[str, Any]
    evidence: Dict[str, Any]
    evaluator_fingerprint: str
    backtest: bool
    tokens_used: Optional[int] = None
    evaluated_at: Optional[str] = None


class CheckResultListResponse(BaseModel):
    results: List[CheckResultResponse]
    total: int


class CompileRequest(BaseModel):
    expression: str = Field(..., min_length=1, max_length=8192)
    output: Optional[str] = None


class BacktestRequest(BaseModel):
    window_days: int = Field(7, ge=1, le=90)


class CompileAssistRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    scope_hint: Optional[str] = Field(None, max_length=500)


def create_assurance_checks_router(logger: BrutorLogger) -> APIRouter:
    """Build the `/admin/assurance-checks/*` router."""
    router = APIRouter(prefix="/admin/assurance-checks", tags=["Assurance Checks"])

    # One tenant-scoping rule for every admin route — see
    # app/core/admin_user/dependencies.py::resolve_tenant_id.
    _tenant_of = resolve_tenant_id

    def _check_of(db: Session, tenant_id: str, check_id: str) -> AssuranceCheck:
        row = (
            db.query(AssuranceCheck)
            .filter(
                AssuranceCheck.id == check_id,
                AssuranceCheck.tenant_id == tenant_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Check not found")
        return row

    def _validate_threshold(
        threshold: Optional[ThresholdInput], output: str
    ) -> Optional[Dict[str, Any]]:
        if threshold is None:
            return None
        if threshold.kind not in THRESHOLD_KINDS:
            raise HTTPException(
                status_code=422, detail=f"Unknown threshold kind '{threshold.kind}'"
            )
        if threshold.op not in THRESHOLD_OPS:
            raise HTTPException(
                status_code=422, detail=f"Unknown threshold op '{threshold.op}'"
            )
        if threshold.severity not in INBOX_SEVERITIES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown threshold severity '{threshold.severity}'",
            )
        out: Dict[str, Any] = {
            "kind": threshold.kind,
            "op": threshold.op,
            "value": threshold.value,
            "severity": threshold.severity,
        }
        if threshold.kind == THRESHOLD_KIND_RATE_OVER:
            if output != CHECK_OUTPUT_BOOLEAN:
                raise HTTPException(
                    status_code=422,
                    detail="rate_over thresholds are defined over boolean checks "
                    "(the rate is the share of true results in the window)",
                )
            if not threshold.window_hours:
                raise HTTPException(
                    status_code=422, detail="rate_over thresholds need window_hours"
                )
            out["window_hours"] = threshold.window_hours
        return out

    def _validate_spec(
        tenant_id: str,
        *,
        tier: str,
        evaluator: Dict[str, Any],
        output: str,
        trigger: Optional[str],
        threshold: Optional[ThresholdInput],
    ) -> tuple[str, Optional[Dict[str, Any]]]:
        """Validate tier/evaluator/output/trigger; returns (trigger, threshold).

        Deterministic expressions go to the core for compile + sample
        evaluation — authoring fails closed if the core is unreachable.
        """
        if tier not in CHECK_TIERS:
            raise HTTPException(status_code=422, detail=f"Unknown tier '{tier}'")
        if output not in CHECK_OUTPUTS:
            raise HTTPException(status_code=422, detail=f"Unknown output '{output}'")

        if tier == CHECK_TIER_DETERMINISTIC:
            resolved_trigger = trigger or CHECK_TRIGGER_ON_FINALIZE
            if resolved_trigger != CHECK_TRIGGER_ON_FINALIZE:
                raise HTTPException(
                    status_code=422,
                    detail="deterministic checks run on_finalize (windowed "
                    "thresholds ride the per-run results)",
                )
            if output == "extraction":
                raise HTTPException(
                    status_code=422,
                    detail="extraction output needs the semantic tier",
                )
            expression = evaluator.get("expression")
            if not expression or not isinstance(expression, str):
                raise HTTPException(
                    status_code=422,
                    detail="deterministic checks need evaluator.expression",
                )
            try:
                # Blocking httpx call. `_validate_spec` stays synchronous
                # (it is pure validation and raises HTTPException); both
                # of its callers are threadpool (`def`) handlers so this
                # round trip never runs on the event-loop thread.
                verdict = core_client.compile_expression(
                    tenant_id, expression, output
                )
            except core_client.CoreUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not verdict.get("ok"):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "expression rejected by the evaluator",
                        "errors": verdict.get("errors", []),
                    },
                )
        else:
            resolved_trigger = trigger or CHECK_TRIGGER_BATCH
            if resolved_trigger != CHECK_TRIGGER_BATCH:
                raise HTTPException(
                    status_code=422, detail="semantic checks run as batch"
                )
            rubric = evaluator.get("rubric")
            if not rubric or not isinstance(rubric, str):
                raise HTTPException(
                    status_code=422, detail="semantic checks need evaluator.rubric"
                )
            sample_rate = evaluator.get("sample_rate", 1.0)
            if not isinstance(sample_rate, (int, float)) or not 0 < sample_rate <= 1:
                raise HTTPException(
                    status_code=422,
                    detail="evaluator.sample_rate must be in (0, 1]",
                )
            if output == CHECK_OUTPUT_CLASSIFICATION and not evaluator.get("labels"):
                raise HTTPException(
                    status_code=422,
                    detail="classification output needs evaluator.labels",
                )

        return resolved_trigger, _validate_threshold(threshold, output)

    def _snapshot(db: Session, row: AssuranceCheck, admin_username: str) -> None:
        db.add(
            AssuranceCheckVersion(
                id=f"ckv-{ULID()}",
                tenant_id=row.tenant_id,
                check_id=row.id,
                version=row.version,
                spec_hash=row.spec_hash,
                spec={
                    "name": row.name,
                    "description_nl": row.description_nl,
                    "scope": row.scope,
                    "tier": row.tier,
                    "evaluator": row.evaluator,
                    "output": row.output,
                    "trigger": row.trigger,
                    "threshold": row.threshold,
                },
                created_by=admin_username,
            )
        )

    def _enabled_cap(db: Session, tenant_id: str) -> int:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        settings = (tenant.settings or {}) if tenant else {}
        cfg = settings.get("assurance_checks") or {}
        cap = cfg.get("max_enabled", DEFAULT_MAX_ENABLED_CHECKS)
        return int(cap) if isinstance(cap, (int, float)) else DEFAULT_MAX_ENABLED_CHECKS

    # ── Authoring aid ───────────────────────────────────────────────────

    @router.post("/compile", summary="Compile-check an expression")
    async def compile_check(
        body: CompileRequest,
        tenant_id: Optional[str] = Query(None),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        try:
            # Blocking HTTP (15s timeout) — off the event loop. See the
            # note at the validate path above.
            return await asyncio.to_thread(
                core_client.compile_expression,
                tenant_id, body.expression, body.output,
            )
        except core_client.CoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/compile-assist",
                 summary="Propose a spec from a natural-language question")
    def compile_assist_endpoint(
        body: CompileAssistRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        from app.services.assurance_checks.nl_assist import compile_assist

        # `compile_assist` makes a judge LLM call and then a blocking
        # compile round trip, both on the sync clients. Same reasoning as
        # the compile path: off the loop.
        return compile_assist(db, tenant_id, body.question, logger, scope_hint=body.scope_hint)

    # ── CRUD ────────────────────────────────────────────────────────────

    @router.get("", response_model=List[CheckResponse], summary="List checks")
    def list_checks(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> List[CheckResponse]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        rows = (
            db.query(AssuranceCheck)
            .filter(AssuranceCheck.tenant_id == tenant_id)
            .order_by(AssuranceCheck.created_at.asc())
            .all()
        )
        return [CheckResponse(**r.to_dict()) for r in rows]

    @router.post("", response_model=CheckResponse, status_code=201,
                 summary="Create a check (version 1)")
    def create_check(
        body: CheckCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> CheckResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        # `_validate_spec` compile-checks deterministic expressions against
        # the core over the BLOCKING httpx client (CP-6). Called directly
        # from this `async def` it runs on the event-loop thread and stalls
        # every other request this worker serves for the whole timeout.
        trigger, threshold = functools.partial(_validate_spec, tenant_id, tier=body.tier, evaluator=body.evaluator, output=body.output, trigger=body.trigger, threshold=body.threshold)()
        clash = (
            db.query(AssuranceCheck)
            .filter(
                AssuranceCheck.tenant_id == tenant_id,
                AssuranceCheck.name == body.name,
            )
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=409, detail=f"A check named '{body.name}' already exists"
            )
        spec = {
            "scope": body.scope,
            "tier": body.tier,
            "evaluator": body.evaluator,
            "output": body.output,
            "trigger": trigger,
            "threshold": threshold,
        }
        row = AssuranceCheck(
            id=f"chk-{ULID()}",
            tenant_id=tenant_id,
            name=body.name,
            description_nl=body.description_nl,
            version=1,
            spec_hash=compute_spec_hash(spec),
            scope=body.scope,
            tier=body.tier,
            evaluator=body.evaluator,
            output=body.output,
            trigger=trigger,
            threshold=threshold,
            route_to_inbox=body.route_to_inbox,
            enabled=False,
            created_by=admin.username,
        )
        db.add(row)
        db.flush()
        _snapshot(db, row, admin.username)
        audit.log(
            action="assurance.check.create", db=db, admin=admin,
            resource_type="assurance_check", resource_id=row.id,
            tenant_id=tenant_id,
            details={"name": row.name, "tier": row.tier, "version": 1},
        )
        db.commit()
        return CheckResponse(**row.to_dict())

    @router.get("/{check_id}", response_model=CheckResponse, summary="Get a check")
    def get_check(
        check_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> CheckResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        return CheckResponse(**_check_of(db, tenant_id, check_id).to_dict())

    @router.patch("/{check_id}", response_model=CheckResponse, summary="Update a check")
    def update_check(
        check_id: str,
        body: CheckUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> CheckResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _check_of(db, tenant_id, check_id)

        spec_changed = any(
            field is not None
            for field in (body.scope, body.evaluator, body.output, body.threshold)
        )
        if body.name is not None and body.name != row.name:
            clash = (
                db.query(AssuranceCheck)
                .filter(
                    AssuranceCheck.tenant_id == tenant_id,
                    AssuranceCheck.name == body.name,
                    AssuranceCheck.id != row.id,
                )
                .first()
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail=f"A check named '{body.name}' already exists",
                )
            row.name = body.name
        if body.description_nl is not None:
            row.description_nl = body.description_nl
        if body.route_to_inbox is not None:
            row.route_to_inbox = body.route_to_inbox

        if spec_changed:
            new_scope = body.scope if body.scope is not None else row.scope
            new_evaluator = (
                body.evaluator if body.evaluator is not None else row.evaluator
            )
            new_output = body.output if body.output is not None else row.output
            threshold_input = (
                body.threshold
                if body.threshold is not None
                else (
                    ThresholdInput(**row.threshold) if row.threshold else None
                )
            )
            # Same blocking-compile dispatch as create_check above.
            trigger, threshold = functools.partial(_validate_spec, tenant_id, tier=row.tier, evaluator=new_evaluator, output=new_output, trigger=None, threshold=threshold_input)()
            row.scope = new_scope
            row.evaluator = new_evaluator
            row.output = new_output
            row.trigger = trigger
            row.threshold = threshold
            row.version = row.version + 1
            row.spec_hash = compute_spec_hash(
                {
                    "scope": row.scope,
                    "tier": row.tier,
                    "evaluator": row.evaluator,
                    "output": row.output,
                    "trigger": row.trigger,
                    "threshold": row.threshold,
                }
            )
            # A new version is unapproved until an admin (re-)enables it.
            row.approved_by = None
            row.enabled = False
            db.flush()
            _snapshot(db, row, admin.username)

        if body.enabled is not None and body.enabled != row.enabled:
            if body.enabled:
                enabled_count = (
                    db.query(AssuranceCheck)
                    .filter(
                        AssuranceCheck.tenant_id == tenant_id,
                        AssuranceCheck.enabled.is_(True),
                        AssuranceCheck.id != row.id,
                    )
                    .count()
                )
                cap = _enabled_cap(db, tenant_id)
                if enabled_count >= cap:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Enabled-check cap reached ({cap}); disable "
                        "another check or raise the tenant's "
                        "assurance_checks.max_enabled setting",
                    )
                row.enabled = True
                row.approved_by = admin.username
            else:
                row.enabled = False

        audit.log(
            action="assurance.check.update", db=db, admin=admin,
            resource_type="assurance_check", resource_id=row.id,
            tenant_id=tenant_id,
            details={
                "name": row.name,
                "version": row.version,
                "spec_changed": spec_changed,
                "enabled": row.enabled,
            },
        )
        db.commit()
        return CheckResponse(**row.to_dict())

    @router.delete("/{check_id}", status_code=204, summary="Delete a check")
    def delete_check(
        check_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> None:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _check_of(db, tenant_id, check_id)
        audit.log(
            action="assurance.check.delete", db=db, admin=admin,
            resource_type="assurance_check", resource_id=row.id,
            tenant_id=tenant_id,
            details={"name": row.name, "version": row.version},
        )
        db.delete(row)
        db.commit()

    # ── Versions / backtest / results ───────────────────────────────────

    @router.get("/{check_id}/versions", response_model=List[CheckVersionResponse],
                summary="List immutable spec versions")
    def list_versions(
        check_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> List[CheckVersionResponse]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        _check_of(db, tenant_id, check_id)
        rows = (
            db.query(AssuranceCheckVersion)
            .filter(
                AssuranceCheckVersion.tenant_id == tenant_id,
                AssuranceCheckVersion.check_id == check_id,
            )
            .order_by(AssuranceCheckVersion.version.desc())
            .all()
        )
        return [CheckVersionResponse(**r.to_dict()) for r in rows]

    @router.post("/{check_id}/backtest", summary="Backtest the current version")
    def backtest_check(
        check_id: str,
        body: BacktestRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _check_of(db, tenant_id, check_id)
        if row.tier != CHECK_TIER_DETERMINISTIC:
            raise HTTPException(
                status_code=422,
                detail="backtest is only defined for deterministic checks",
            )
        try:
            # The one that hurts most: `core_client.backtest` uses the
            # blocking httpx client with a 120s timeout. Awaited on the
            # loop, a single slow backtest froze every other request on
            # this worker for two minutes — and gunicorn.conf.py runs two
            # workers by default, so two concurrent backtests stalled the
            # whole admin API.
            summary = core_client.backtest(tenant_id, row.id, row.version, body.window_days)
        except core_client.CoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if summary.get("ok"):
            version_row = (
                db.query(AssuranceCheckVersion)
                .filter(
                    AssuranceCheckVersion.check_id == row.id,
                    AssuranceCheckVersion.version == row.version,
                )
                .first()
            )
            if version_row is not None:
                version_row.backtest_summary = {
                    "evaluated": summary.get("evaluated"),
                    "breaches": summary.get("breaches"),
                    "errors": summary.get("errors"),
                    "window_days": summary.get("window_days"),
                }
            audit.log(
                action="assurance.check.backtest", db=db, admin=admin,
                resource_type="assurance_check", resource_id=row.id,
                tenant_id=tenant_id,
                details={
                    "version": row.version,
                    "window_days": body.window_days,
                    "evaluated": summary.get("evaluated"),
                    "breaches": summary.get("breaches"),
                },
            )
            db.commit()
        return summary

    @router.get("/{check_id}/results", response_model=CheckResultListResponse,
                summary="Recent results for a check")
    def list_results(
        check_id: str,
        tenant_id: Optional[str] = Query(None),
        backtest: Optional[bool] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> CheckResultListResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        _check_of(db, tenant_id, check_id)
        q = db.query(AssuranceCheckResult).filter(
            AssuranceCheckResult.tenant_id == tenant_id,
            AssuranceCheckResult.check_id == check_id,
        )
        if backtest is not None:
            q = q.filter(AssuranceCheckResult.backtest.is_(backtest))
        total = q.count()
        rows = (
            q.order_by(AssuranceCheckResult.evaluated_at.desc()).limit(limit).all()
        )
        return CheckResultListResponse(
            results=[CheckResultResponse(**r.to_dict()) for r in rows],
            total=total,
        )

    return router
