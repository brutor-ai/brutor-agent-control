# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Response policies + replay visibility

    GET    /v1/admin/ai-systems/{id}/response-policies
    POST   /v1/admin/ai-systems/{id}/response-policies
    POST   /v1/admin/ai-systems/{id}/response-policies/preview
    PATCH  /v1/admin/response-policies/{id}
    DELETE /v1/admin/response-policies/{id}
    GET    /v1/admin/ai-systems/{id}/responses
    POST   /v1/admin/ai-systems/{id}/restore
    GET    /v1/admin/ai-systems/{id}/replay
    POST   /v1/admin/ai-systems/{id}/replay/suite      — freeze a suite
    POST   /v1/admin/ai-systems/{id}/replay/run        — request a run
    GET    /v1/admin/ai-systems/{id}/health            — the worst-of score
    GET    /v1/admin/ai-systems/{id}/assurance         — live report
    POST   /v1/admin/ai-systems/{id}/assurance         — capture a snapshot
    GET    /v1/admin/ai-systems/{id}/assurance/history — captured snapshots

## The preview is not a convenience

`POST .../response-policies/preview` returns the **blast radius** and what
the policy would do to the system's current state, and the UI is expected
to show it before the policy is armed. This is a requirement rather
than a nicety, and the reason is specific: autonomy resolves min-wins *down*
the resource-group tree, so a policy attached to a team-level group
restricts every inheriting system beneath it. An operator who discovers
that after an incident will not trust the feature again.

The preview is deliberately available **before** the policy exists, so the
affected set can be shown while it is still being written.

## Refusals are first-class

`GET .../responses` returns findings that were *considered and refused*
alongside the ones that fired, with the reason. Silence after a critical
drift finding is indistinguishable from a broken feature, and an operator
who cannot tell "no policy matched" from "the loop is down" will assume the
worst — correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Any, Dict, List, Literal, Optional, Union

from app.services.ai_system_signal_profile import (
    NOT_APPLICABLE,
    relaxations,
)

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from ulid import ULID

from app.api.v1.models.admin.ai_system_composition import (
    DependencyCap,
    DependencyHealthEntry,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_replay import (
    REPLAY_RUNNING,
    AISystemReplayCase,
    AISystemReplayRun,
    AISystemReplaySuite,
)
from app.database.models.ai_system_response_policy import (
    AISystemResponsePolicy,
    PolicyValidationError,
    SEVERITIES,
    SIGNAL_KINDS,
    validate_actions,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.system_operation_log import (
    ACTION_DRIFT_RESPONSE,
    ACTOR_TYPE_SYSTEM_ADMIN,
    ACTOR_TYPE_TENANT_ADMIN,
    SystemOperationLog,
)
from app.services.ai_system_response.reach import limitation_for
from app.services.ai_system_response.actuator import (
    inheriting_descendants,
    resolved_autonomy,
    restore as do_restore,
)
from app.services.ai_system_response.evaluator import plan_response


#: How old the materialised health row may be before a read recomputes it.
#: Five minutes: cheap enough to absorb on a page load, fresh enough that a
#: demo, an incident review, or an auditor is never looking at yesterday.
HEALTH_FRESHNESS_SECONDS = 300


class PolicyResponse(BaseModel):
    id: str
    ai_system_id: str
    name: str
    description: Optional[str] = None
    enabled: bool
    signal_kind: str
    drift_class: Optional[str] = None
    severity_at_least: str
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    allow_suspend: bool
    require_human_to_restore: bool
    response_cooldown_seconds: int
    last_fired_at: Optional[str] = None
    last_fired_drift_event_id: Optional[str] = None
    fire_count: int
    is_actuating: bool
    target_autonomy: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PolicyListResponse(BaseModel):
    policies: List[PolicyResponse]
    total: int
    inherited_count: int = Field(
        0,
        description=(
            "Policies that apply to this system but are attached to an "
            "ancestor. Shown because an operator debugging why a system was "
            "throttled needs to find the policy that did it, and it may not "
            "be on the system they are looking at."
        ),
    )


class PolicyWriteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    enabled: bool = True
    signal_kind: str = "drift"
    drift_class: Optional[str] = None
    severity_at_least: str = "high"
    actions: List[Dict[str, Any]]
    allow_suspend: bool = False
    require_human_to_restore: bool = True
    response_cooldown_seconds: int = Field(3600, ge=0, le=86400)


class PolicyPatchRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    enabled: Optional[bool] = None
    drift_class: Optional[str] = None
    severity_at_least: Optional[str] = None
    actions: Optional[List[Dict[str, Any]]] = None
    allow_suspend: Optional[bool] = None
    require_human_to_restore: Optional[bool] = None
    response_cooldown_seconds: Optional[int] = Field(None, ge=0, le=86400)


class PreviewRequest(BaseModel):
    """A candidate policy, which need not exist yet."""

    signal_kind: str = "drift"
    drift_class: Optional[str] = None
    severity_at_least: str = "high"
    actions: List[Dict[str, Any]]
    allow_suspend: bool = False
    require_human_to_restore: bool = True
    #: The severity to simulate. Defaults to the policy's own floor — the
    #: question an operator is asking is "what happens when this fires".
    simulate_severity: Optional[str] = None


class PreviewResponse(BaseModel):
    ai_system_id: str
    current_autonomy: Optional[str] = None
    blast_radius: Optional[Dict[str, Any]] = None
    would_do: List[Dict[str, Any]] = Field(default_factory=list)
    would_refuse: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ResponseRecord(BaseModel):
    drift_event_id: str
    detected_at: Optional[str] = None
    drift_class: str
    severity: str
    attributed_cause: Optional[str] = None
    acted: bool
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    refusals: List[Dict[str, Any]] = Field(default_factory=list)
    limitations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "What an action that was taken does not cover, e.g. callers "
            "approval_required does not hold."
        ),
    )
    blast_radius: Optional[Dict[str, Any]] = None
    responded_at: Optional[str] = None


class ResponseHistory(BaseModel):
    responses: List[ResponseRecord]
    total: int
    acted_count: int
    pending_count: int = Field(
        0,
        description=(
            "Findings the loop has not considered yet. A number that stays "
            "high is how an operator notices the closed loop has stopped "
            "running."
        ),
    )


class BuildSuiteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    target_case_count: int = Field(30, ge=1, le=500)
    window_days: int = Field(30, ge=1, le=365)
    pass_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    #: Bind it to the promotion gate straight away. Off by default: an
    #: operator should look at the coverage report before a suite starts
    #: deciding whether releases ship.
    bind_to_gate: bool = False


class BuildSuiteResponse(BaseModel):
    suite_id: str
    name: str
    eligible_runs: int
    cases_created: int
    coverage: Dict[str, int] = Field(default_factory=dict)
    coverage_gaps: List[str] = Field(
        default_factory=list,
        description=(
            "Strata present in the population but absent from the sample. A "
            "gate is only as good as what its evidence covers."
        ),
    )
    bound_to_gate: bool = False
    warnings: List[str] = Field(default_factory=list)


class RequestRunResponse(BaseModel):
    replay_run_id: str
    status: str
    note: str = Field(
        "Queued. The proxy executes replay runs; poll GET .../replay for the result.",
        description="Execution happens in gateway-core, which picks the row up.",
    )


class RecentRun(BaseModel):
    run_id: str
    terminal_state: str
    # The run's class on the three questions, its declared
    # outcome and, for a failed run, why it failed.
    run_class: Optional[str] = None
    outcome_id: Optional[str] = None
    outcome_conformance: Optional[str] = None
    error_cause: Optional[str] = None
    ended_at: Optional[str] = None
    failed: bool


class RecentRunsVerdict(BaseModel):
    """THE down rule over the system's own latest finished runs."""

    verdict: Optional[Literal["ok", "degraded", "down"]] = Field(
        None,
        description=(
            "`down` (every run read failed) makes the state `failing`; "
            "`degraded` caps reliability at 50%. None: no finished runs yet."
        ),
    )
    considered: int = 0
    failed: int = 0
    rule_runs: int = Field(3, description="How many latest finished runs the rule reads.")
    latest: List[RecentRun] = Field(default_factory=list, description="Newest first.")
    window_success_rate: Optional[float] = Field(
        None, description="Completed / finished runs over the health window, for contrast."
    )
    window_hours: Optional[int] = None


class HealthWindows(BaseModel):
    """What each part of the verdict is computed over, so the UI can say it."""

    health_period_hours: int = Field(
        ...,
        description=(
            "Every component (reliability, cost, conformance, …) is graded "
            "over this trailing window. Fixed: it does NOT follow a "
            "dashboard's range selector."
        ),
    )
    recent_runs: int = Field(
        ..., description="The latest this-many finished runs are judged for `failing`."
    )
    freshness_seconds: int = Field(
        ..., description="A stored verdict older than this is recomputed on read."
    )


class HealthResponse(BaseModel):
    ai_system_id: str
    health_state: str
    computed_at: Optional[str] = Field(
        None,
        description=(
            "When this verdict was computed. A health state without a "
            "timestamp invites trusting a number of unknown age."
        ),
    )
    health_score: Optional[float] = None
    liveness_score: Optional[float] = None
    behaviour_score: Optional[float] = None
    reliability_score: Optional[float] = None
    cost_score: Optional[float] = None
    conformance_score: Optional[float] = None
    # Persisted by the scorer; declared here so Pydantic does not strip it.
    # A new field must be added to `to_dict()`, to this model, AND to the
    # field-by-field builder below.
    oversight_score: Optional[float] = None
    system_kind: Optional[str] = Field(
        None,
        description="The kind whose signal profile was applied to this verdict.",
    )
    signal_levels: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-signal {score, level}. `level` is enforced | advisory | "
            "not_applicable. Render `not_applicable` as 'n/a' — never as a "
            "zero and never as a pass."
        ),
    )
    profile_relaxations: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Signals this system's kind does not fully enforce, with the "
            "reason. Disclosed so that choosing a kind cannot quietly drop "
            "an inconvenient signal."
        ),
    )
    chain_integrity_pct: Optional[float] = None
    baseline_state: Optional[str] = None
    runs_24h: int = 0
    open_drift_events: int = 0
    reasons: List[str] = Field(
        default_factory=list,
        description=(
            "Why the state is what it is, in the operator's words. A state "
            "with no explanation sends someone hunting through four panels."
        ),
    )
    is_trustworthy: bool = Field(
        False,
        description=(
            "Whether enough of this system's runs were observed intact for "
            "the score to mean anything."
        ),
    )
    signal_basis: Literal["runs", "segments"] = Field(
        "runs",
        description=(
            "`segments` when the system roots no runs in the "
            "window but served calls as a shared service: its signals are "
            "computed from the calls it served."
        ),
    )
    dependency_health: List[DependencyHealthEntry] = Field(
        default_factory=list,
        description=(
            "Every declared dependency's health as this system "
            "sees it: `tested` for shared services, `assumed` for external agents."
        ),
    )
    dependency_cap: Optional[DependencyCap] = Field(
        None,
        description=(
            "Set when a critical shared-service dependency that is not healthy "
            "caps this system at degraded (reason `dependency`). Stored with "
            "health_state by the same computation (computed_at)."
        ),
    )
    recent_runs: Optional[RecentRunsVerdict] = Field(
        None,
        description=(
            "The down rule over the latest finished runs, stored with "
            "health_state by the same computation. None for a shared service "
            "(judged on the calls it serves) or a row not yet recomputed."
        ),
    )
    windows: Optional[HealthWindows] = None


class CaptureRequest(BaseModel):
    period_hours: int = Field(720, ge=0, le=8760)


class AssuranceSummary(BaseModel):
    id: str
    ai_system_id: str
    period_hours: int
    health_state: Optional[str] = None
    contract_version: Optional[int] = None
    contract_hash: Optional[str] = None
    trigger: str
    captured_by: Optional[str] = None
    created_at: Optional[str] = None
    # Which snapshots are certificates, and what they concluded.
    signed: bool = False
    verdict_status: Optional[str] = None


class AssuranceHistory(BaseModel):
    reports: List[AssuranceSummary]
    total: int


class RestoreRequest(BaseModel):
    to_level: Optional[str] = Field(
        None,
        description=(
            "The level to restore to. Null clears the override entirely, "
            "letting the system inherit again."
        ),
    )
    drift_event_id: Optional[str] = None


class ReplayStatus(BaseModel):
    suite_id: Optional[str] = None
    suite_name: Optional[str] = None
    bound: bool = False
    replayable: bool = True
    not_replayable_reason: Optional[str] = None
    enabled: bool = True
    pass_threshold: Optional[float] = None
    case_count: int = 0
    coverage: Dict[str, int] = Field(default_factory=dict)
    last_run: Optional[Dict[str, Any]] = None
    gate_state: str = Field(
        "no_suite",
        description="no_suite | never_run | not_replayable | running | passing | failing",
    )


def _latest_suite_id(db: Session, tenant_id: str, ai_system_id: str) -> Optional[str]:
    """The most recently built suite for a system, bound or not.

    Ordered by id rather than a timestamp: suite ids are ULIDs, so they sort
    lexicographically by creation time, and this avoids depending on a
    `created_at` that the model is free to stop populating.
    """
    row = (
        db.query(AISystemReplaySuite.id)
        .filter(
            AISystemReplaySuite.tenant_id == tenant_id,
            AISystemReplaySuite.ai_system_id == ai_system_id,
        )
        .order_by(AISystemReplaySuite.id.desc())
        .first()
    )
    return row[0] if row else None


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def create_ai_system_response_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Response"])

    def _load_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == ai_system_id,
                ResourceGroup.tenant_id == tenant_id,
            )
            .first()
        )
        if system is None:
            raise HTTPException(
                status_code=404, detail=f"AI System {ai_system_id!r} not found"
            )
        if system.group_type != GroupType.AI_SYSTEM:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{ai_system_id!r} is a {system.group_type} group, not an AI "
                    f"System — response policies act on a governed workload"
                ),
            )
        return system

    def _load_policy(
        db: Session, tenant_id: str, policy_id: str
    ) -> AISystemResponsePolicy:
        p = (
            db.query(AISystemResponsePolicy)
            .filter(
                AISystemResponsePolicy.id == policy_id,
                AISystemResponsePolicy.tenant_id == tenant_id,
            )
            .first()
        )
        if p is None:
            raise HTTPException(
                status_code=404, detail=f"Response policy {policy_id!r} not found"
            )
        return p

    def _validate(actions, allow_suspend: bool) -> List[Dict[str, Any]]:
        try:
            return validate_actions(actions, allow_suspend=allow_suspend)
        except PolicyValidationError as exc:
            # 422, not 400: the request was well-formed but names an action
            # the platform will not arm, and the message says which.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Policies
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/response-policies",
        response_model=PolicyListResponse,
        summary="Response policies applying to this AI System",
    )
    def list_policies(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        include_inherited: bool = Query(True),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> PolicyListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        if include_inherited:
            # The management listing shows disabled policies too — see
            # `policies_in_scope`. Using the actuator's view here hid every
            # policy an operator had turned off, including the one they were
            # about to turn back on.
            from app.services.ai_system_response.actuator import policies_in_scope

            rows = policies_in_scope(db, system, enabled_only=False)
        else:
            rows = (
                db.query(AISystemResponsePolicy)
                .filter(
                    AISystemResponsePolicy.tenant_id == resolved,
                    AISystemResponsePolicy.ai_system_id == ai_system_id,
                )
                .order_by(AISystemResponsePolicy.name)
                .all()
            )

        return PolicyListResponse(
            policies=[
                PolicyResponse(
                    **{
                        k: v
                        for k, v in p.to_dict().items()
                        if k not in ("tenant_id",)
                    }
                )
                for p in rows
            ],
            total=len(rows),
            inherited_count=sum(1 for p in rows if p.ai_system_id != ai_system_id),
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/response-policies",
        response_model=PolicyResponse,
        summary="Arm a response policy",
    )
    def create_policy(
        ai_system_id: str,
        payload: PolicyWriteRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> PolicyResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        if payload.signal_kind not in SIGNAL_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"signal_kind must be one of {sorted(SIGNAL_KINDS)}",
            )
        if payload.severity_at_least not in SEVERITIES:
            raise HTTPException(
                status_code=422,
                detail=f"severity_at_least must be one of {sorted(SEVERITIES)}",
            )
        actions = _validate(payload.actions, payload.allow_suspend)

        policy = AISystemResponsePolicy(
            id=f"rsp-{ULID()}",
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            name=payload.name,
            description=payload.description,
            enabled=payload.enabled,
            signal_kind=payload.signal_kind,
            drift_class=payload.drift_class,
            severity_at_least=payload.severity_at_least,
            actions=actions,
            allow_suspend=payload.allow_suspend,
            require_human_to_restore=payload.require_human_to_restore,
            response_cooldown_seconds=payload.response_cooldown_seconds,
            created_by=getattr(admin, "username", None),
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return PolicyResponse(
            **{k: v for k, v in policy.to_dict().items() if k != "tenant_id"}
        )

    @router.patch(
        "/admin/response-policies/{policy_id}",
        response_model=PolicyResponse,
        summary="Update a response policy",
    )
    def update_policy(
        policy_id: str,
        payload: PolicyPatchRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> PolicyResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        policy = _load_policy(db, resolved, policy_id)

        fields = payload.model_dump(exclude_unset=True)
        # Validated against the *resulting* allow_suspend, not the stored
        # one: turning suspend off in the same request that adds a suspend
        # action must be refused, not silently ordered.
        allow_suspend = fields.get("allow_suspend", policy.allow_suspend)
        if "actions" in fields:
            fields["actions"] = _validate(fields["actions"], allow_suspend)
        elif "allow_suspend" in fields and not allow_suspend:
            _validate(policy.actions, allow_suspend)

        for key, value in fields.items():
            setattr(policy, key, value)
        db.commit()
        db.refresh(policy)
        return PolicyResponse(
            **{k: v for k, v in policy.to_dict().items() if k != "tenant_id"}
        )

    @router.delete(
        "/admin/response-policies/{policy_id}",
        summary="Disarm a response policy",
    )
    def delete_policy(
        policy_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        policy = _load_policy(db, resolved, policy_id)
        db.delete(policy)
        db.commit()
        return {"deleted": policy_id}

    # ------------------------------------------------------------------
    # Preview — required before arming
    # ------------------------------------------------------------------

    @router.post(
        "/admin/ai-systems/{ai_system_id}/response-policies/preview",
        response_model=PreviewResponse,
        summary="What this policy would do, and to how many systems",
        description=(
            "Available before the policy exists, so the affected set can be "
            "shown while it is still being written. Autonomy resolves "
            "min-wins down the resource-group tree, so a policy on a parent "
            "restricts every inheriting system beneath it."
        ),
    )
    def preview_policy(
        ai_system_id: str,
        payload: PreviewRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> PreviewResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        actions = _validate(payload.actions, payload.allow_suspend)

        # A detached instance: never added to the session, so a preview
        # cannot accidentally arm anything.
        candidate = AISystemResponsePolicy(
            id="preview",
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            name="preview",
            enabled=True,
            signal_kind=payload.signal_kind,
            drift_class=payload.drift_class,
            severity_at_least=payload.severity_at_least,
            actions=actions,
            allow_suspend=payload.allow_suspend,
            require_human_to_restore=payload.require_human_to_restore,
        )

        current = resolved_autonomy(db, system)
        descendants = inheriting_descendants(db, system)

        plan = plan_response(
            policies=[candidate],
            signal_kind=payload.signal_kind,
            severity=payload.simulate_severity or payload.severity_at_least,
            drift_class=payload.drift_class,
            current_autonomy=current,
            inheriting_descendants=descendants,
            target_system_id=ai_system_id,
        )

        warnings: List[str] = []
        if plan.blast_radius and plan.blast_radius.is_wide:
            warnings.append(
                f"This policy acts on {plan.blast_radius.total} systems: "
                f"{ai_system_id} and {len(descendants)} inheriting below it."
            )
        if candidate.target_autonomy == "suspended":
            warnings.append(
                "Suspending denies every call from every actor in these systems, "
                "including read-only ones."
            )
        note = limitation_for(
            db, resolved, candidate.target_autonomy, [ai_system_id, *descendants]
        )
        if note:
            warnings.append(note["detail"])
        if not payload.require_human_to_restore:
            warnings.append(
                "Automatic restore is enabled. This flaps: the signal clears "
                "because the system was throttled, the throttle lifts, and the "
                "signal returns."
            )

        return PreviewResponse(
            ai_system_id=ai_system_id,
            current_autonomy=current,
            blast_radius=plan.blast_radius.to_dict() if plan.blast_radius else None,
            would_do=[a.to_dict() for a in plan.actions],
            would_refuse=[r.to_dict() for r in plan.refusals],
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # History and restore
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/responses",
        response_model=ResponseHistory,
        summary="What the closed loop did, and what it refused to do",
    )
    def response_history(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ResponseHistory:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        rows = (
            db.query(AISystemDriftEvent)
            .filter(
                AISystemDriftEvent.tenant_id == resolved,
                AISystemDriftEvent.ai_system_id == ai_system_id,
            )
            .order_by(AISystemDriftEvent.detected_at.desc())
            .limit(limit)
            .all()
        )

        records: List[ResponseRecord] = []
        acted = 0
        pending = 0
        for e in rows:
            taken = e.response_taken or {}
            if not taken:
                pending += 1
            did_act = bool(taken.get("actions"))
            if did_act:
                acted += 1
            records.append(
                ResponseRecord(
                    drift_event_id=e.id,
                    detected_at=e.detected_at.isoformat() if e.detected_at else None,
                    drift_class=e.drift_class,
                    severity=e.severity,
                    attributed_cause=e.attributed_cause,
                    acted=did_act,
                    policy_id=taken.get("policy_id"),
                    policy_name=taken.get("policy_name"),
                    actions=taken.get("actions") or [],
                    refusals=taken.get("refusals") or [],
                    limitations=taken.get("limitations") or [],
                    blast_radius=taken.get("blast_radius"),
                    responded_at=taken.get("responded_at"),
                )
            )

        return ResponseHistory(
            responses=records,
            total=len(records),
            acted_count=acted,
            pending_count=pending,
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/restore",
        summary="Undo an automatic tightening",
        description=(
            "Human-driven by design. Automatic restore flaps: the signal "
            "clears because the system was throttled, the throttle lifts, "
            "and the signal returns."
        ),
    )
    def restore_autonomy(
        ai_system_id: str,
        payload: RestoreRequest = Body(default=RestoreRequest()),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        before = system.autonomy_level
        do_restore(
            db,
            system,
            actor_admin_id=str(getattr(admin, "id", "unknown")),
            actor_type=(
                ACTOR_TYPE_TENANT_ADMIN
                if admin_type == "tenant"
                else ACTOR_TYPE_SYSTEM_ADMIN
            ),
            to_level=payload.to_level,
            drift_event_id=payload.drift_event_id,
        )
        db.commit()
        return {
            "ai_system_id": ai_system_id,
            "from": before,
            "to": payload.to_level,
            "resolved_autonomy": resolved_autonomy(db, system),
        }

    # ------------------------------------------------------------------
    # Replay visibility
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/replay",
        response_model=ReplayStatus,
        summary="Replay suite state and what the promotion gate makes of it",
    )
    def replay_status(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        suite_id: Optional[str] = Query(
            None,
            description=(
                "Inspect a specific suite instead of the one bound to the gate. "
                "Mirrors the same parameter on .../replay/run — without it an "
                "unbound suite can be built and executed but never read, which "
                "makes 'review the coverage before you bind' a dead end."
            ),
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ReplayStatus:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        bound_suite_id = getattr(system, "replay_suite_id", None)
        # Precedence: an explicitly requested suite, else the gate-bound one,
        # else the most recently built suite for this system.
        #
        # That last fallback is what keeps "review the coverage before you
        # bind" workable. Building without binding is the default, there is no
        # endpoint to list suites, and nothing binds one after the fact — so
        # without it a suite becomes unreachable the moment the operator
        # reloads the page, and the only way back to a readable suite is to
        # bind one sight-unseen.
        #
        # `bound` stays keyed to what the gate actually reads either way: an
        # unbound suite must never render as though it were gating promotion.
        if not suite_id:
            suite_id = bound_suite_id or _latest_suite_id(db, resolved, ai_system_id)
        if not suite_id:
            return ReplayStatus(bound=False, gate_state="no_suite")
        is_bound = suite_id == bound_suite_id

        suite = (
            db.query(AISystemReplaySuite)
            .filter(
                AISystemReplaySuite.id == suite_id,
                AISystemReplaySuite.tenant_id == resolved,
            )
            .first()
        )
        if suite is None:
            raise HTTPException(
                status_code=404,
                detail=f"Replay suite {suite_id!r} is bound but no longer exists",
            )

        cases = (
            db.query(AISystemReplayCase)
            .filter(
                AISystemReplayCase.tenant_id == resolved,
                AISystemReplayCase.suite_id == suite.id,
            )
            .all()
        )
        coverage: Dict[str, int] = {}
        for c in cases:
            key = c.stratum or "unlabelled"
            coverage[key] = coverage.get(key, 0) + 1

        latest = (
            db.query(AISystemReplayRun)
            .filter(
                AISystemReplayRun.tenant_id == resolved,
                AISystemReplayRun.suite_id == suite.id,
            )
            .order_by(AISystemReplayRun.started_at.desc())
            .first()
        )

        # `gate_state` answers "what does the promotion gate make of this
        # system", so when nothing is bound the answer is `no_suite` — however
        # good the inspected suite's own last run was. Reporting `passing`
        # while no suite gates anything would put a green state on a system
        # nothing is actually checking, which is the precise failure this
        # feature exists to prevent. The suite's own verdict is still returned
        # in `last_run` for a caller that wants to read it.
        if not is_bound:
            gate_state = "no_suite"
        elif not suite.replayable:
            gate_state = "not_replayable"
        elif latest is None:
            gate_state = "never_run"
        elif latest.status == REPLAY_RUNNING and latest.finished_at is None:
            # In flight: not a pass, but not a failure either. The console
            # showed "Last replay did not pass" for a run the proxy had not
            # started yet, which sent operators looking for a regression.
            gate_state = "running"
        elif latest.passes(suite.pass_threshold):
            gate_state = "passing"
        else:
            gate_state = "failing"

        return ReplayStatus(
            suite_id=suite.id,
            suite_name=suite.name,
            bound=is_bound,
            replayable=suite.replayable,
            not_replayable_reason=suite.not_replayable_reason,
            enabled=suite.enabled,
            pass_threshold=suite.pass_threshold,
            case_count=len(cases),
            coverage=dict(sorted(coverage.items())),
            last_run=latest.to_dict() if latest else None,
            gate_state=gate_state,
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/replay/suite",
        response_model=BuildSuiteResponse,
        summary="Freeze recent production runs into a replay suite",
        description=(
            "Sampling is stratified across terminal states, step-count "
            "deciles and tool signatures, rarest first — 'the last 50 runs' "
            "encodes the happy path and misses the cases a config change "
            "actually breaks."
        ),
    )
    def build_suite_endpoint(
        ai_system_id: str,
        payload: BuildSuiteRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> BuildSuiteResponse:
        from app.services.ai_system_suite_builder import build_suite

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        suite, report = build_suite(
            db,
            tenant_id=resolved,
            ai_system_id=ai_system_id,
            name=payload.name,
            description=payload.description,
            target_case_count=payload.target_case_count,
            window_days=payload.window_days,
            pass_threshold=payload.pass_threshold,
            created_by=getattr(admin, "username", None),
        )

        warnings: List[str] = []
        if report["cases_created"] == 0:
            warnings.append(
                "No eligible runs in the window, so the suite is empty. An "
                "empty suite fails rather than passing, which will block "
                "promotion if you bind it."
            )
        elif report["cases_created"] < payload.target_case_count:
            warnings.append(
                f"Only {report['cases_created']} of {payload.target_case_count} "
                f"cases could be sampled from {report['eligible_runs']} eligible runs."
            )
        if report["coverage_gaps"]:
            warnings.append(
                f"{len(report['coverage_gaps'])} stratum/strata in the traffic are "
                f"not represented: {', '.join(report['coverage_gaps'][:5])}"
            )

        bound = False
        if payload.bind_to_gate:
            system.replay_suite_id = suite.id
            bound = True

        db.commit()
        return BuildSuiteResponse(
            suite_id=suite.id,
            name=suite.name,
            eligible_runs=report["eligible_runs"],
            cases_created=report["cases_created"],
            coverage=report["coverage"],
            coverage_gaps=report["coverage_gaps"],
            bound_to_gate=bound,
            warnings=warnings,
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/replay/run",
        response_model=RequestRunResponse,
        summary="Request a replay run",
        description=(
            "Writes the request; the proxy executes it. The two are joined "
            "by the row rather than a call, so neither service goes down "
            "with the other."
        ),
    )
    def request_replay_run(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        suite_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> RequestRunResponse:
        from app.database.models.ai_system_replay import (
            REPLAY_RUNNING,
            TRIGGER_MANUAL,
        )
        from app.services import ai_system_contract_service as contracts

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        target = suite_id or getattr(system, "replay_suite_id", None)
        if not target:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No replay suite bound to this AI System, and none named. "
                    "Build one first."
                ),
            )
        suite = (
            db.query(AISystemReplaySuite)
            .filter(
                AISystemReplaySuite.id == target,
                AISystemReplaySuite.tenant_id == resolved,
            )
            .first()
        )
        if suite is None:
            raise HTTPException(
                status_code=404, detail=f"Replay suite {target!r} not found"
            )

        active = contracts.current_contract(db, resolved, ai_system_id)
        run = AISystemReplayRun(
            id=f"rpr-{ULID()}",
            tenant_id=resolved,
            suite_id=suite.id,
            ai_system_id=ai_system_id,
            contract_id=active.id if active else None,
            status=REPLAY_RUNNING,
            triggered_by=TRIGGER_MANUAL,
            triggered_by_actor=getattr(admin, "username", None),
        )
        db.add(run)
        db.commit()
        return RequestRunResponse(replay_run_id=run.id, status=REPLAY_RUNNING)

    # ------------------------------------------------------------------
    # Health + the Assurance Report
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/health",
        response_model=HealthResponse,
        summary="One number, five components, always decomposable",
        description=(
            "Worst-of, not an average: a system that is cheap, fast and dead "
            "is not 80% healthy. Returns `unknown` with no score when there "
            "is too little data or the call chain could not be trusted."
        ),
    )
    def system_health(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        refresh: bool = Query(
            False, description="Recompute rather than reading the materialised row."
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> HealthResponse:
        from app.database.models.ai_system_health import (
            AISystemHealth,
            MIN_CHAIN_INTEGRITY_FOR_SCORE,
        )
        from app.services.ai_system_assurance import (
            DEFAULT_PERIOD_HOURS,
            compute_health,
            upsert_health,
        )
        from app.services.ai_system_down_rule import DOWN_AFTER_CONSECUTIVE_FAILURES

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        row = (
            db.query(AISystemHealth)
            .filter(
                AISystemHealth.tenant_id == resolved,
                AISystemHealth.ai_system_id == ai_system_id,
            )
            .first()
        )
        # Staleness rule: the materialised row exists so tiles do not
        # recompute per render, but a health verdict is only worth what its
        # timestamp is. The scheduled refresh is off unless a tenant opts in,
        # which previously meant this endpoint could serve a day-old state
        # with no cue — a tile that said "learning, 100% observed" about a
        # system whose last 24 hours it had never seen. Recompute when the
        # row is older than the freshness window; always return computed_at
        # so the UI can say "as of".
        computed_at = row.computed_at if row is not None else None
        if computed_at is not None and computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)  # SQLite reads naive
        stale = (
            computed_at is not None
            and (datetime.now(timezone.utc) - computed_at)
            > timedelta(seconds=HEALTH_FRESHNESS_SECONDS)
        )
        if refresh or row is None or stale:
            row = upsert_health(db, system)
            db.commit()

        # Read with the SAME functions the scorer used, so the
        # basis and levels shown are the ones the row was scored under.
        from app.services import ai_system_service_signals as service_signals
        from app.services.ai_system_dependency_health import dependency_health

        ctx = service_signals.signal_context(
            db, resolved, ai_system_id,
            datetime.now(timezone.utc) - timedelta(hours=DEFAULT_PERIOD_HOURS),
        )
        levels = service_signals.signal_profile(getattr(system, "system_kind", None), ctx)
        dependencies = dependency_health(db, system)

        data = row.to_dict()
        return HealthResponse(
            ai_system_id=ai_system_id,
            health_state=data["health_state"],
            health_score=data["health_score"],
            liveness_score=data["liveness_score"],
            behaviour_score=data["behaviour_score"],
            reliability_score=data["reliability_score"],
            cost_score=data["cost_score"],
            conformance_score=data["conformance_score"],
            oversight_score=data.get("oversight_score"),
            system_kind=getattr(system, "system_kind", None),
            # Derived from `system_kind` at read time rather than persisted:
            # a stored profile would go stale the moment an operator changed
            # the kind, and a stale profile is worse than none.
            signal_levels={
                sig: {
                    # A not-applicable signal reports no score at all. Passing
                    # its raw number through would invite the UI to render it,
                    # and a signal we deliberately do not judge must not appear
                    # to have been judged.
                    "score": (
                        None if lvl == NOT_APPLICABLE else data.get(f"{sig}_score")
                    ),
                    "level": lvl,
                }
                for sig, lvl in levels.items()
            },
            profile_relaxations=relaxations(getattr(system, "system_kind", None)),
            chain_integrity_pct=data["chain_integrity_pct"],
            baseline_state=data["baseline_state"],
            runs_24h=data["runs_24h"],
            open_drift_events=data["open_drift_events"],
            reasons=data["reasons"],
            is_trustworthy=data["is_trustworthy"],
            computed_at=data.get("computed_at"),
            signal_basis=ctx.basis,
            dependency_health=[DependencyHealthEntry(**d) for d in dependencies],
            # The cap STORED with `health_state` by the
            # same computation (upsert_health). Never recomputed here: a live
            # cap could disagree with the stored state until the next sweep.
            dependency_cap=(
                DependencyCap(**row.dependency_cap) if row.dependency_cap else None
            ),
            recent_runs=(
                RecentRunsVerdict(**data["recent_runs"]) if data.get("recent_runs") else None
            ),
            windows=HealthWindows(
                health_period_hours=DEFAULT_PERIOD_HOURS,
                recent_runs=DOWN_AFTER_CONSECUTIVE_FAILURES,
                freshness_seconds=HEALTH_FRESHNESS_SECONDS,
            ),
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/assurance",
        summary="The live Assurance Report",
        description=(
            "Answers the four guarantees with evidence, and states in its "
            "`not_covered` section what the platform did **not** observe."
        ),
    )
    def assurance_report(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(720, ge=0, le=8760),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict[str, Any]:
        from app.services.ai_system_assurance import build_report

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        return build_report(db, system, period_hours=period_hours)

    @router.post(
        "/admin/ai-systems/{ai_system_id}/assurance",
        response_model=AssuranceSummary,
        summary="Capture an immutable assurance snapshot",
        description=(
            "Frozen once written. Recomputing it later would answer 'what is "
            "true now' rather than 'what was true then', and evidence that "
            "changes when the system does is not evidence."
        ),
    )
    def capture_assurance(
        ai_system_id: str,
        payload: CaptureRequest = Body(default=CaptureRequest()),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> AssuranceSummary:
        from app.services.ai_system_assurance import capture

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        row = capture(
            db,
            system,
            period_hours=payload.period_hours,
            captured_by=getattr(admin, "username", None),
        )
        db.commit()
        db.refresh(row)
        data = row.to_dict(include_report=False)
        data.pop("tenant_id", None)
        return AssuranceSummary(**data)

    @router.get(
        "/admin/ai-systems/{ai_system_id}/assurance/history",
        response_model=AssuranceHistory,
        summary="Captured assurance snapshots",
    )
    def assurance_history(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> AssuranceHistory:
        from app.database.models.ai_system_health import AISystemAssuranceReport

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        rows = (
            db.query(AISystemAssuranceReport)
            .filter(
                AISystemAssuranceReport.tenant_id == resolved,
                AISystemAssuranceReport.ai_system_id == ai_system_id,
            )
            .order_by(AISystemAssuranceReport.created_at.desc())
            .limit(limit)
            .all()
        )
        summaries = []
        for r in rows:
            data = r.to_dict(include_report=False)
            data.pop("tenant_id", None)
            summaries.append(AssuranceSummary(**data))
        return AssuranceHistory(reports=summaries, total=len(summaries))

    @router.get(
        "/admin/ai-systems/{ai_system_id}/assurance/history/{report_id}/export",
        summary="A captured snapshot as a portable Assurance Certificate",
        description=(
            "The canonical `ai.brutor/ai-system-assurance-report` document: "
            "the frozen report, its digest, and — when the deployment has an "
            "audit key — the Ed25519 attestation. Verifiable offline with "
            "sha256sum and any Ed25519 library; no API call back to us."
        ),
    )
    def export_assurance_certificate(
        ai_system_id: str,
        report_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict[str, Any]:
        from app.database.models.ai_system_health import AISystemAssuranceReport
        from app.services.ai_system_assurance import certificate_document

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        row = (
            db.query(AISystemAssuranceReport)
            .filter(
                AISystemAssuranceReport.id == report_id,
                AISystemAssuranceReport.tenant_id == resolved,
                AISystemAssuranceReport.ai_system_id == ai_system_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Snapshot {report_id!r} not found"
            )
        return certificate_document(row)

    return router
