# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System contracts + lifecycle gate

    GET   /v1/admin/ai-systems/{id}/contracts            — version history
    GET   /v1/admin/ai-systems/{id}/contracts/current    — what governs runs now
    GET   /v1/admin/ai-systems/{id}/contracts/{version}  — one version + its closure
    POST  /v1/admin/ai-systems/{id}/contracts            — mint from live config
    GET   /v1/admin/ai-systems/{id}/contracts/{ver}/diff — what changed, by direction
    POST  /v1/admin/contracts/{id}/approve
    POST  /v1/admin/contracts/{id}/reject
    POST  /v1/admin/contracts/{id}/promote
    GET   /v1/admin/ai-systems/{id}/outcomes             — declared outcome catalogue
    PUT   /v1/admin/ai-systems/{id}/outcomes             — replace it (validated, 422 with paths)
    GET   /v1/admin/ai-systems/{id}/lifecycle            — what each move needs
    POST  /v1/admin/ai-systems/{id}/lifecycle            — attempt a transition

## The gate is readable before it is enforced

`GET /lifecycle` returns the full requirement list with each item marked
satisfied or not, for **every** stage the system could move to. That is
deliberate: the UI shows an operator what is missing before they click,
rather than a red toast after. A gate you can only discover by failing it
is a gate people learn to resent.

The POST enforces the same evaluation, because a UI check is a convenience
and never a control.

## Minting is idempotent

Posting a contract when nothing has changed returns the existing one with
`created: false` rather than piling up identical versions. The version
history has to stay readable — it is the thing an auditor scrolls.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_asset import LIFECYCLE_STAGES
from app.database.models.ai_system_contract import AISystemContract
from app.database.models.ai_system_lifecycle_event import (
    EVENT_STAGE_TRANSITION,
    AISystemLifecycleEvent,
    new_event_id,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services import ai_system_contract_service as contracts
from app.services import ai_system_envelope as envelope_service
from app.services import ai_system_outcome_catalogue as outcome_catalogue
from app.services import ai_system_lifecycle_gate as gate
from app.services.ai_system_closure import resolve_closure


class ContractResponse(BaseModel):
    id: str
    ai_system_id: str
    version: int
    contract_hash: str
    short_hash: str
    status: str
    diff_from_version: Optional[int] = None
    diff_summary: Optional[Dict[str, Any]] = None
    lifecycle_stage_at_promotion: Optional[str] = None
    dry_run_summary: Optional[Dict[str, Any]] = None
    replay_run_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    promoted_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    is_active: bool
    is_approved: bool
    # Only on single-contract responses; the list view omits it because a
    # closure is large and nobody reads twenty at once.
    resolved_closure: Optional[Dict[str, Any]] = None


class ContractListResponse(BaseModel):
    contracts: List[ContractResponse]
    total: int
    active_version: Optional[int] = None
    config_drifted: bool = Field(
        False,
        description=(
            "Live configuration no longer matches the active contract. This "
            "is the conformance question, and it is computable only "
            "because contracts are generated rather than authored."
        ),
    )


class MintResponse(BaseModel):
    contract: ContractResponse
    created: bool = Field(
        ...,
        description=(
            "False when live config already matched the latest contract. "
            "Minting an identical version on every save would bury the real "
            "changes in a history nobody can read."
        ),
    )


class OffendingCredential(BaseModel):
    credential_ref: Optional[str] = None
    credential_kind: str
    credential_assurance: Optional[str] = None
    calls: int
    last_seen: Optional[str] = None


class RequirementResponse(BaseModel):
    key: str
    label: str
    satisfied: bool
    detail: Optional[str] = None
    #: `workload_identity_enforced` when unmet: the credentials below the
    #: level, newest first (at most 20), and their total.
    offending_credentials: Optional[List[OffendingCredential]] = None
    offending_total: Optional[int] = None


class TransitionOption(BaseModel):
    to_stage: str
    allowed: bool
    requirements: List[RequirementResponse]
    unmet: List[RequirementResponse]


class SuccessorResponse(BaseModel):
    """Where a deprecated system's capability went, if anywhere."""

    successor_system_id: Optional[str] = None
    successor_name: Optional[str] = None
    no_successor_declared: Optional[bool] = None


class LifecycleResponse(BaseModel):
    ai_system_id: str
    current_stage: Optional[str] = None
    eu_ai_act_risk_tier: Optional[str] = None
    autonomy_level: Optional[str] = None
    transitions: List[TransitionOption]
    #: Only meaningful once the deprecate question has been put.
    succession: Optional[SuccessorResponse] = None
    #: AI Systems in this tenant that could be named as a successor —
    #: so the console can ask the question instead of answering it.
    candidate_successors: List[Dict[str, Any]] = Field(default_factory=list)
    #: Set once deprecated: when this system becomes retirable, so the
    #: console can count down instead of only ever saying "not yet".
    retirement: Optional[Dict[str, Any]] = None
    #: The transitions this system has been through, newest first:
    #: who approved each one, who recorded it, and when. Stage transitions
    #: ONLY — the console reads `history[0]` as "in this stage since".
    history: List[Dict[str, Any]] = Field(default_factory=list)
    #: Lifecycle events that move no stage, newest first:
    #: `agent.new_release` for each release a member agent was first seen
    #: running (`details`: agent, name, version, build, trust, approved).
    events: List[Dict[str, Any]] = Field(default_factory=list)


def _lifecycle_rows(
    db: Session, tenant_id: str, ai_system_id: str, *, stage_transitions: bool, limit: int = 20
) -> List[Dict[str, Any]]:
    q = db.query(AISystemLifecycleEvent).filter(
        AISystemLifecycleEvent.tenant_id == tenant_id,
        AISystemLifecycleEvent.ai_system_id == ai_system_id,
    )
    q = q.filter(
        AISystemLifecycleEvent.event_type == EVENT_STAGE_TRANSITION
        if stage_transitions
        else AISystemLifecycleEvent.event_type != EVENT_STAGE_TRANSITION
    )
    return [
        e.to_dict()
        for e in q.order_by(
            AISystemLifecycleEvent.created_at.desc(), AISystemLifecycleEvent.id.desc()
        ).limit(limit)
    ]


class ApproveRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class TransitionRequest(BaseModel):
    to_stage: str
    #: The accountable approver. Defaults to the admin performing the
    #: transition; name someone else only when a person who is not logged in
    #: signed off. The recorder is always the session admin.
    approved_by: Optional[str] = None
    successor_id: Optional[str] = None
    no_successor: bool = False


class EnvelopeTerm(BaseModel):
    """One term an operating envelope can declare, and what a value of it
    must be. A declaration outside these is refused (422)."""

    label: str = Field(..., description="The term's name for a person.")
    help: str = Field(..., description="What the term bounds and how it is used.")
    direction: Literal["max", "min"] = Field(
        ..., description="Which way is stricter: a `max` term is a ceiling, a `min` term a floor."
    )
    unit: Literal["usd", "count", "seconds", "rate"] = Field(
        ..., description="`usd`: dollars. `count` and `seconds`: whole numbers. `rate`: a share from 0 to 1."
    )
    signal: Literal["cost", "reliability"] = Field(..., description="The health component a breach degrades.")
    enforced: bool = Field(
        ..., description="The gateway refuses the call that would pass the term. False: graded in the report only."
    )
    graded: bool = Field(
        ..., description="The Assurance Report grades the term against what was observed. False: enforced and stated only."
    )
    parameterized: bool = Field(
        ..., description="The term names an outcome: declared as `<term>:<outcome>[.<variant>]`, once per outcome."
    )
    whole_number: bool = Field(..., description="The value must be a whole number.")
    minimum: Optional[float] = Field(None, description="The smallest value accepted (inclusive).")
    exclusive_minimum: Optional[float] = Field(None, description="The value must be greater than this.")
    maximum: Optional[float] = Field(
        None,
        description=(
            "The largest value accepted (inclusive). `new_runs_window_seconds`: 2678400, 31 days."
        ),
    )
    default: Optional[float] = Field(
        None,
        description=(
            "What applies when the term is not declared and the term it `requires` is: "
            "`new_runs_window_seconds` is 3600 (an hour) for a `max_new_runs_per_window` declared alone."
        ),
    )
    requires: Optional[str] = Field(
        None,
        description=(
            "A term that must be declared with this one: `new_runs_window_seconds` is the window "
            "`max_new_runs_per_window` counts in, and is refused without it."
        ),
    )


class EnvelopeResponse(BaseModel):
    ai_system_id: str
    operating_envelope: Optional[Dict[str, float]] = None
    known_terms: Dict[str, EnvelopeTerm] = Field(
        ..., description="Every term an envelope can declare, by its key in `operating_envelope`."
    )
    config_drifted: bool = Field(
        False,
        description=(
            "Setting or changing the envelope changes the resolved closure, "
            "so the active contract no longer matches live config until a "
            "new version is minted and approved."
        ),
    )


class EnvelopeUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    operating_envelope: Optional[Dict[str, float]] = Field(
        None,
        description=(
            "Declared operating-envelope terms ({term: bound}). None or {} "
            "clears the declaration. Unknown terms are refused — a term "
            "nobody evaluates is a promise nobody keeps."
        ),
    )


class OutcomeExpect(BaseModel):
    """What a run reporting the outcome must look like: a cheap outcome needs evidence."""

    max_actions: Optional[int] = None
    max_model_calls: Optional[int] = None
    max_cost_usd: Optional[float] = None
    requires_capabilities: Optional[List[str]] = None
    forbids_capabilities: Optional[List[str]] = None


class OutcomeVariantDecl(BaseModel):
    id: str
    label: str
    description: Optional[str] = None
    expect: Optional[OutcomeExpect] = None


class OutcomeDecl(BaseModel):
    """One declared outcome — normalised (`ai_system_outcome_catalogue`)."""

    id: str
    label: str
    description: Optional[str] = None
    counts_toward_baseline: bool
    expect: Optional[OutcomeExpect] = None
    variants: Optional[List[OutcomeVariantDecl]] = None


class OutcomesResponse(BaseModel):
    ai_system_id: str
    outcomes: Optional[List[OutcomeDecl]] = Field(
        None,
        description="The live catalogue, normalised (what the next contract will carry); null = none",
    )
    active_outcomes: Optional[List[OutcomeDecl]] = Field(
        None,
        description=(
            "The catalogue in the ACTIVE contract — what reported outcomes are judged "
            "against now. Null with no active contract or none in it (every id undeclared)."
        ),
    )
    active_contract_version: Optional[int] = None
    reportable_ids: List[str] = Field(
        default_factory=list,
        description="Every X-Brutor-Outcome-Id the live catalogue declares (<outcome>[.<variant>])",
    )
    config_drifted: bool = Field(
        False,
        description=(
            "Changing the catalogue changes the resolved closure: the active contract no "
            "longer matches live config until a new version is minted and approved."
        ),
    )


class OutcomesUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    outcomes: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "The full catalogue (full-replace). null or [] clears it. Validated: ids "
            "[a-z0-9_-]{1,48} unique, labels 1–128 chars, ≤ 32 outcomes and ≤ 32 variants "
            "each, `expect` keys max_actions | max_model_calls | max_cost_usd | "
            "requires_capabilities | forbids_capabilities. Errors → 422 with field paths."
        ),
    )


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _contract_dict(c: AISystemContract, *, include_closure: bool) -> Dict[str, Any]:
    data = c.to_dict()
    if not include_closure:
        data.pop("resolved_closure", None)
    data.pop("tenant_id", None)
    return data


def create_ai_system_contracts_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Contracts"])

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
                    f"System — contracts describe a governed workload, not an "
                    f"access group"
                ),
            )
        return system

    def _load_contract(db: Session, tenant_id: str, contract_id: str) -> AISystemContract:
        c = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.id == contract_id,
                AISystemContract.tenant_id == tenant_id,
            )
            .first()
        )
        if c is None:
            raise HTTPException(
                status_code=404, detail=f"Contract {contract_id!r} not found"
            )
        return c

    def _tenant_policy(db: Session, tenant_id: str) -> Optional[Dict[str, Any]]:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        return getattr(tenant, "ai_system_gate_policy", None) if tenant else None

    @router.get(
        "/admin/ai-systems/gate-policy/vocabulary",
        summary="Valid transitions and requirement keys for a tenant gate policy",
        description=(
            "The vocabulary an operator may use in "
            "`tenants.ai_system_gate_policy`, served from "
            "the gate's own tables so an editor cannot drift from what the "
            "gate actually enforces. Also returns the current policy and the "
            "defaults each transition falls back to when unset."
        ),
    )
    def gate_policy_vocabulary(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict[str, Any]:
        from app.services.ai_system_lifecycle_gate import (
            DEFAULT_REQUIREMENTS,
            IDENTITY_FLOOR_TRANSITIONS,
            REQ_HUMAN_APPROVAL,
            REQ_WORKLOAD_IDENTITY,
            REQUIREMENT_LABELS,
        )

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        return {
            "transitions": [
                {
                    "key": key,
                    "from_stage": key.split("->")[0],
                    "to_stage": key.split("->")[1],
                    "default_requirements": list(reqs),
                }
                for key, reqs in sorted(DEFAULT_REQUIREMENTS.items())
            ],
            "requirements": [
                {"key": k, "label": v} for k, v in sorted(REQUIREMENT_LABELS.items())
            ],
            # Stated rather than implied: a tenant may relax a transition to
            # nothing, but the gate re-adds human approval for a high-risk
            # system, and workload identity on every way into `active`,
            # afterwards. An editor that let an operator "remove" either
            # without saying so would misrepresent what was saved.
            "immutable_floors": [
                {
                    "condition": "eu_ai_act_risk_tier == 'high'",
                    "always_required": [REQ_HUMAN_APPROVAL],
                    "note": (
                        "Re-added by the gate after any tenant override. A policy "
                        "may omit it; the gate will still demand it."
                    ),
                },
                {
                    "condition": "to_stage == 'active'",
                    "transitions": list(IDENTITY_FLOOR_TRANSITIONS),
                    "always_required": [REQ_WORKLOAD_IDENTITY],
                    "note": "Re-added by the gate after any tenant override.",
                },
            ],
            "current_policy": _tenant_policy(db, resolved),
        }

    # ------------------------------------------------------------------
    # Operating envelope — the "what it should achieve" half of the
    # contract. Stored on the system, folded into the closure, and
    # therefore into the hash the approver signs.
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/envelope",
        response_model=EnvelopeResponse,
        summary="The declared operating envelope, and the term vocabulary",
    )
    def get_envelope(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> EnvelopeResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        return EnvelopeResponse(
            ai_system_id=ai_system_id,
            operating_envelope=envelope_service.validate_envelope(
                system.operating_envelope
            ),
            known_terms=envelope_service.known_terms(),
            config_drifted=contracts.is_config_drifted(db, system),
        )

    @router.put(
        "/admin/ai-systems/{ai_system_id}/envelope",
        response_model=EnvelopeResponse,
        summary="Declare or update the operating envelope",
        description=(
            "Full-replace semantics, like the other governance PATCHes: the "
            "stored envelope becomes exactly what is sent. Changing it makes "
            "live config drift from the active contract — mint and approve a "
            "new version to bring the promise under signature."
        ),
    )
    def put_envelope(
        ai_system_id: str,
        payload: EnvelopeUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> EnvelopeResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        try:
            normalized = envelope_service.validate_envelope(
                payload.operating_envelope
            )
        except envelope_service.EnvelopeValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        system.operating_envelope = normalized
        db.flush()
        db.commit()

        logger.info(
            "AI System operating envelope updated",
            extra={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "terms": sorted(normalized) if normalized else [],
                "actor": getattr(admin, "username", None),
            },
        )
        return EnvelopeResponse(
            ai_system_id=ai_system_id,
            operating_envelope=normalized,
            known_terms=envelope_service.known_terms(),
            config_drifted=contracts.is_config_drifted(db, system),
        )

    # ------------------------------------------------------------------
    # Declared outcomes — how a run may end. Stored on the
    # system, folded into the closure (section `outcomes`), signed off with
    # the contract.
    # ------------------------------------------------------------------

    def _outcomes_response(db: Session, system: ResourceGroup) -> OutcomesResponse:
        live = outcome_catalogue.validate_catalogue(system.declared_outcomes)
        active = contracts.current_contract(db, system.tenant_id, system.id)
        return OutcomesResponse(
            ai_system_id=system.id,
            outcomes=live,
            active_outcomes=((active.resolved_closure or {}).get("outcomes") if active else None),
            active_contract_version=active.version if active else None,
            reportable_ids=outcome_catalogue.all_ids(live),
            config_drifted=contracts.is_config_drifted(db, system),
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/outcomes",
        response_model=OutcomesResponse,
        summary="The declared outcome catalogue (live and active)",
    )
    def get_outcomes(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> OutcomesResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        return _outcomes_response(db, system)

    @router.put(
        "/admin/ai-systems/{ai_system_id}/outcomes",
        response_model=OutcomesResponse,
        summary="Declare or replace the outcome catalogue",
        description=(
            "Full-replace semantics: the stored catalogue becomes exactly what is sent, "
            "normalised. Changing it makes live config drift from the active contract — "
            "mint, approve and promote a new version to have outcomes judged against it."
        ),
    )
    def put_outcomes(
        ai_system_id: str,
        payload: OutcomesUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> OutcomesResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        try:
            normalized = outcome_catalogue.validate_catalogue(payload.outcomes)
        except outcome_catalogue.CatalogueValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors)

        system.declared_outcomes = normalized
        db.flush()
        db.commit()
        logger.info(
            "AI System outcome catalogue updated",
            extra={
                "tenant_id": resolved,
                "ai_system_id": ai_system_id,
                "outcomes": [o["id"] for o in normalized or []],
                "actor": getattr(admin, "username", None),
            },
        )
        return _outcomes_response(db, system)

    # ------------------------------------------------------------------
    # Contracts
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/contracts",
        response_model=ContractListResponse,
        summary="Contract version history for an AI System",
    )
    def list_contracts(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ContractListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        rows = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.tenant_id == resolved,
                AISystemContract.ai_system_id == ai_system_id,
            )
            .order_by(AISystemContract.version.desc())
            .all()
        )
        active = next((c for c in rows if c.is_active), None)
        return ContractListResponse(
            contracts=[
                ContractResponse(**_contract_dict(c, include_closure=False))
                for c in rows
            ],
            total=len(rows),
            active_version=active.version if active else None,
            config_drifted=contracts.is_config_drifted(db, system),
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/contracts/current",
        response_model=ContractResponse,
        summary="The contract governing this system's runs right now",
    )
    def current_contract(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ContractResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        active = contracts.current_contract(db, resolved, ai_system_id)
        if active is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No active contract — this system's runs are governed by "
                    "live configuration that has never been pinned"
                ),
            )
        return ContractResponse(**_contract_dict(active, include_closure=True))

    @router.post(
        "/admin/ai-systems/{ai_system_id}/contracts",
        response_model=MintResponse,
        summary="Mint a contract from the current resolved configuration",
    )
    def mint_contract(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        force: bool = Query(
            False,
            description=(
                "Mint even when the live configuration already matches the "
                "latest contract. The re-attestation case: an approver can "
                "only sign a draft, so a new owner who wants their own name "
                "against unchanged config needs a fresh version to approve. "
                "The resulting version has an empty diff by design."
            ),
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> MintResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        contract, created = contracts.mint(
            db, system, created_by=getattr(admin, "username", None), force=force
        )
        db.commit()
        db.refresh(contract)
        return MintResponse(
            contract=ContractResponse(**_contract_dict(contract, include_closure=True)),
            created=created,
        )

    @router.get(
        # NOTE: declared AFTER /contracts/current. FastAPI matches in
        # declaration order, so a `{version}` route placed above it would
        # swallow the literal "current" and 422 on the int parse.
        "/admin/ai-systems/{ai_system_id}/contracts/{version}",
        response_model=ContractResponse,
        summary="One contract version, including the closure it stamped",
        description=(
            "The list endpoint omits `resolved_closure` because closures are "
            "large and a version list is a navigation aid. This returns ONE "
            "version with the closure attached, so an approver can read what "
            "they are signing — the models, MCP servers, skills, agents, "
            "guardrails, policies and merged limits frozen into that version."
        ),
    )
    def get_contract_version(
        ai_system_id: str,
        version: int,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> ContractResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        contract = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.tenant_id == resolved,
                AISystemContract.ai_system_id == ai_system_id,
                AISystemContract.version == version,
            )
            .first()
        )
        if contract is None:
            raise HTTPException(
                status_code=404, detail=f"Contract v{version} not found"
            )
        # The STORED closure, never a recomputed one. The hash pins these
        # bytes; re-deriving from today's config would show the approver
        # something they never signed.
        return ContractResponse(**_contract_dict(contract, include_closure=True))

    @router.get(
        "/admin/ai-systems/{ai_system_id}/contracts/{version}/diff",
        summary="What changed in this contract version, split by direction",
    )
    def contract_diff(
        ai_system_id: str,
        version: int,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _load_system(db, resolved, ai_system_id)

        contract = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.tenant_id == resolved,
                AISystemContract.ai_system_id == ai_system_id,
                AISystemContract.version == version,
            )
            .first()
        )
        if contract is None:
            raise HTTPException(
                status_code=404, detail=f"Contract v{version} not found"
            )
        # The stored diff, not a recomputed one: an approver must be able to
        # see exactly what they were shown.
        return {
            "ai_system_id": ai_system_id,
            "version": version,
            "diff_from_version": contract.diff_from_version,
            "summary": contract.diff_summary or {
                "widened": [], "narrowed": [], "changed": [],
                "widened_count": 0, "narrowed_count": 0, "changed_count": 0,
                "has_widened_permissions": False,
            },
        }

    @router.post(
        "/admin/contracts/{contract_id}/approve",
        response_model=ContractResponse,
        summary="Record approval of a draft contract",
    )
    def approve_contract(
        contract_id: str,
        tenant_id: Optional[str] = Query(None),
        payload: ApproveRequest = Body(default=ApproveRequest()),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> ContractResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        contract = _load_contract(db, resolved, contract_id)
        try:
            contracts.approve(
                db, contract, approved_by=getattr(admin, "username", "unknown")
            )
        except contracts.ContractError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.commit()
        db.refresh(contract)
        return ContractResponse(**_contract_dict(contract, include_closure=False))

    @router.post(
        "/admin/contracts/{contract_id}/reject",
        response_model=ContractResponse,
        summary="Refuse a draft contract",
    )
    def reject_contract(
        contract_id: str,
        payload: RejectRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> ContractResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        contract = _load_contract(db, resolved, contract_id)
        try:
            contracts.reject(db, contract, reason=payload.reason)
        except contracts.ContractError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.commit()
        db.refresh(contract)
        return ContractResponse(**_contract_dict(contract, include_closure=False))

    @router.post(
        "/admin/contracts/{contract_id}/promote",
        response_model=ContractResponse,
        summary="Make this contract the one governing runs",
    )
    def promote_contract(
        contract_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> ContractResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        contract = _load_contract(db, resolved, contract_id)
        system = _load_system(db, resolved, contract.ai_system_id)
        try:
            contracts.promote(db, contract, lifecycle_stage=system.lifecycle_stage)
        except contracts.ContractError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.commit()
        # The promotion is an epoch boundary in the tenant
        # log. Sealed after the commit so the core attributes the record to
        # the new epoch; best-effort (a failure is a capture_gap finding,
        # never a failed promotion).
        from app.services.compliance.sealing_hooks import seal_epoch_boundary

        seal_epoch_boundary(db, contract)
        db.commit()
        db.refresh(contract)
        return ContractResponse(**_contract_dict(contract, include_closure=False))

    # ------------------------------------------------------------------
    # Lifecycle gate
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/contracts/{version}/export",
        summary="A contract version as a portable AI System Contract document",
        description=(
            "The canonical `ai.brutor/ai-system-contract` document: closure, "
            "hash, approval trail — self-contained, so a third party can "
            "check that the closure hashes to what the approval names "
            "without an API call back to us."
        ),
    )
    def export_contract(
        ai_system_id: str,
        version: int,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict[str, Any]:
        from app import standards

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        row = (
            db.query(AISystemContract)
            .filter(
                AISystemContract.tenant_id == resolved,
                AISystemContract.ai_system_id == ai_system_id,
                AISystemContract.version == version,
            )
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Contract v{version} not found for this system",
            )
        return standards.contract_document(row, system)

    @router.get(
        "/admin/ai-systems/{ai_system_id}/lifecycle",
        response_model=LifecycleResponse,
        summary="What each lifecycle transition requires, and what is missing",
    )
    def lifecycle_options(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> LifecycleResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        policy = _tenant_policy(db, resolved)

        options = []
        for stage in sorted(LIFECYCLE_STAGES):
            if stage == system.lifecycle_stage:
                continue
            if not gate.is_known_transition(system.lifecycle_stage, stage):
                continue
            decision = gate.evaluate(db, system, stage, tenant_policy=policy)
            options.append(
                TransitionOption(
                    to_stage=stage,
                    allowed=decision.allowed,
                    requirements=[
                        RequirementResponse(**r.to_dict()) for r in decision.requirements
                    ],
                    unmet=[RequirementResponse(**r.to_dict()) for r in decision.unmet],
                )
            )

        closure = resolve_closure(db, system)

        successor_name = None
        if system.successor_system_id:
            successor_name = (
                db.query(ResourceGroup.name)
                .filter(ResourceGroup.id == system.successor_system_id)
                .scalar()
            )

        # Every other live AI System in the tenant is a candidate. Offered
        # eagerly because the console has to ask "superseded by what?" at the
        # moment of deprecation, and a picker with nothing in it is a picker
        # an operator clicks past.
        candidates = (
            db.query(ResourceGroup.id, ResourceGroup.name, ResourceGroup.lifecycle_stage)
            .filter(
                ResourceGroup.tenant_id == resolved,
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
                ResourceGroup.id != ai_system_id,
                ResourceGroup.is_active.is_(True),
            )
            .order_by(ResourceGroup.name)
            .all()
        )

        return LifecycleResponse(
            ai_system_id=ai_system_id,
            current_stage=system.lifecycle_stage,
            eu_ai_act_risk_tier=system.eu_ai_act_risk_tier,
            autonomy_level=closure.get("autonomy_level"),
            transitions=options,
            succession=SuccessorResponse(
                successor_system_id=system.successor_system_id,
                successor_name=successor_name,
                no_successor_declared=system.no_successor_declared,
            ),
            candidate_successors=[
                {"id": c.id, "name": c.name, "lifecycle_stage": c.lifecycle_stage}
                for c in candidates
            ],
            retirement=(
                gate.retirement_readiness(db, system)
                if system.lifecycle_stage == "deprecated"
                else None
            ),
            history=_lifecycle_rows(db, resolved, ai_system_id, stage_transitions=True),
            events=_lifecycle_rows(db, resolved, ai_system_id, stage_transitions=False),
        )

    @router.post(
        "/admin/ai-systems/{ai_system_id}/lifecycle",
        summary="Attempt a lifecycle transition",
        description=(
            "Refuses with the **full** list of missing evidence rather than "
            "the first failure. A gate that reports one problem at a time "
            "sends an operator round the loop once per requirement."
        ),
    )
    def transition(
        ai_system_id: str,
        payload: TransitionRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)

        if payload.to_stage not in LIFECYCLE_STAGES:
            raise HTTPException(
                status_code=422,
                detail=f"to_stage must be one of {sorted(LIFECYCLE_STAGES)}",
            )

        # A successor has to be a real AI System in the same tenant, and
        # cannot be the system itself — an unchecked id would record a
        # dangling pointer that reads as a migration path and isn't one.
        if payload.successor_id:
            if payload.successor_id == ai_system_id:
                raise HTTPException(
                    status_code=422,
                    detail="A system cannot succeed itself",
                )
            successor = (
                db.query(ResourceGroup)
                .filter(
                    ResourceGroup.id == payload.successor_id,
                    ResourceGroup.tenant_id == resolved,
                    ResourceGroup.group_type == GroupType.AI_SYSTEM,
                )
                .first()
            )
            if successor is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"successor_id {payload.successor_id!r} is not an AI "
                        f"System in this tenant"
                    ),
                )

        # The admin clicking IS a named human. The gate's "named approver"
        # therefore defaults to them; a different name is an on-behalf-of
        # declaration and is kept as such, beside the recorder.
        recorder = getattr(admin, "username", None) or "unknown"
        approver = (payload.approved_by or "").strip() or recorder

        decision = gate.evaluate(
            db,
            system,
            payload.to_stage,
            tenant_policy=_tenant_policy(db, resolved),
            approved_by=approver,
            successor_id=payload.successor_id,
            no_successor=payload.no_successor,
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": decision.refusal_message(),
                    "decision": decision.to_dict(),
                },
            )

        previous = system.lifecycle_stage
        system.lifecycle_stage = payload.to_stage

        # Record the succession answer rather than discarding it once the
        # gate has been satisfied. Without this a deprecated system cannot
        # say where its capability went.
        if payload.successor_id:
            system.successor_system_id = payload.successor_id
            system.no_successor_declared = False
        elif payload.no_successor:
            system.successor_system_id = None
            system.no_successor_declared = True

        db.add(
            AISystemLifecycleEvent(
                id=new_event_id(),
                tenant_id=resolved,
                ai_system_id=ai_system_id,
                from_stage=previous,
                to_stage=payload.to_stage,
                approved_by=approver,
                recorded_by=recorder,
                recorded_by_id=getattr(admin, "id", None),
                successor_system_id=payload.successor_id,
                no_successor=bool(payload.no_successor),
            )
        )
        db.flush()
        db.commit()

        logger.info(
            "AI System lifecycle transition",
            extra={
                "ai_system_id": ai_system_id,
                "tenant_id": resolved,
                "from_stage": previous,
                "to_stage": payload.to_stage,
                "actor": getattr(admin, "username", None),
            },
        )
        return {
            "ai_system_id": ai_system_id,
            "from_stage": previous,
            "to_stage": payload.to_stage,
            "approved_by": approver,
            "recorded_by": recorder,
            "decision": decision.to_dict(),
        }

    return router
