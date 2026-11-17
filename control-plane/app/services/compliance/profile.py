# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The compliance profile per AI System

A neutral **core** plus a **frameworks** block keyed by registry id:

    core:       operator_role, jurisdictions[], entities{provider, deployer,
                representative}, dates{developed, placed_on_market,
                put_into_service}, external_registrations[{framework_id,
                registry, id}], sensitivity{personal_data, special_category,
                phi, financial, minors}, interacts_with_natural_persons,
                generates_synthetic_content, automated_decisions_about_persons,
                two_phase_actions, inference_records, salt_digests,
                oversight_assignment, synthetic_marking, notes
    frameworks: {"eu-ai-act": {role, risk_tier, annex, annex_iii_area,
                 conformity_assessment, <declared references>}, …}

Validation is pydantic. The EU AI Act block is strict (its vocabulary is
this package's); every other block is validated against its catalog's role
and classification vocabularies, with the rest open for the fields each
catalog's DECLARED statements cite — values are scalars or short lists.

**Legacy columns.** `resource_groups.eu_ai_act_role` / `eu_ai_act_risk_tier`
stay the EU AI Act board's source of truth: they are read INTO the
`eu-ai-act` block of every view and written through on every save.

**Sealing.** Every save seals an `ai.brutor.declaration` whose payload (the
saved profile, digest only) makes each DECLARED statement cite a sealed
declaration. A save the core cannot seal is still saved, reported
`sealed: false` with the error, and its `declaration_record_id` is cleared
— a stale record id would cite a declaration that no longer matches.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Any, Dict, List, Literal, Optional, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database.models.ai_asset import RISK_TIERS
from app.database.models.ai_system_compliance_profile import (
    OPERATOR_ROLES,
    AISystemComplianceProfile,
)
from app.database.models.compliance_framework import ComplianceFramework
from app.database.models.resource_group import EU_AI_ACT_ROLES, ResourceGroup
from app.services.compliance import offering
from app.services.compliance.sealing import SealOutcome

EU_AI_ACT_ID = "eu-ai-act"
MAX_TEXT = 2000
MAX_LIST = 50
_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

Text = Optional[str]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entities(_Strict):
    provider: Text = None
    deployer: Text = None
    representative: Text = None


class Dates(_Strict):
    developed: Optional[date] = None
    placed_on_market: Optional[date] = None
    put_into_service: Optional[date] = None


class ExternalRegistration(_Strict):
    framework_id: str
    registry: Text = None
    id: str = Field(..., min_length=1, max_length=255)


class Sensitivity(_Strict):
    personal_data: Optional[bool] = None
    special_category: Optional[bool] = None
    phi: Optional[bool] = None
    financial: Optional[bool] = None
    minors: Optional[bool] = None


class CoreProfile(_Strict):
    operator_role: Optional[Literal["builder", "operator", "vendor_of", "reseller"]] = None
    jurisdictions: Optional[List[str]] = Field(None, max_length=MAX_LIST)
    entities: Optional[Entities] = None
    dates: Optional[Dates] = None
    external_registrations: Optional[List[ExternalRegistration]] = Field(None, max_length=MAX_LIST)
    sensitivity: Optional[Sensitivity] = None
    interacts_with_natural_persons: Optional[bool] = None
    generates_synthetic_content: Optional[bool] = None
    automated_decisions_about_persons: Optional[bool] = None
    two_phase_actions: Optional[bool] = None
    inference_records: Optional[bool] = None
    salt_digests: Optional[bool] = None
    oversight_assignment: Optional[str] = Field(None, max_length=MAX_TEXT)
    synthetic_marking: Optional[str] = Field(None, max_length=MAX_TEXT)
    notes: Optional[str] = Field(None, max_length=MAX_TEXT)

    @field_validator("jurisdictions")
    @classmethod
    def _jurisdictions(cls, v):
        if v is not None and not all(isinstance(j, str) and j.strip() for j in v):
            raise ValueError("jurisdictions must be non-empty strings")
        return v

    @field_validator(
        "interacts_with_natural_persons", "generates_synthetic_content", "automated_decisions_about_persons",
        "two_phase_actions", "inference_records", "salt_digests", mode="before",
    )
    @classmethod
    def _strict_bool(cls, v):
        if v is not None and not isinstance(v, bool):
            raise ValueError("must be a boolean")
        return v


#: The EU AI Act facts that are a choice, not prose. The console offers exactly
#: these (brutor-gateway-admin `src/lib/complianceEvidenceTypes.ts`:
#: EU_ANNEXES, EU_ANNEX_III_AREAS, EU_CONFORMITY_ASSESSMENTS), so a value
#: outside them is one the console cannot show: the field reads "not declared"
#: and the next save from the console erases it (the demo declared
#: `annex: "III"` and an area in prose, and its sealed profile read "not
#: declared" on screen). Detail such as the point within an area
#: ("5(b)") belongs in `core.notes`.
EU_ANNEXES = ("none", "annex_i", "annex_iii")
EU_ANNEX_III_AREAS = ("1", "2", "3", "4", "5", "6", "7", "8")
EU_CONFORMITY_ASSESSMENTS = ("internal", "notified_body")


def _one_of(field: str, allowed: tuple):
    def check(cls, v):
        if v is not None and v not in allowed:
            raise ValueError(f"{field} must be one of {list(allowed)}")
        return v

    return field_validator(field)(classmethod(check))


class EuAiActBlock(_Strict):
    role: Optional[str] = None
    risk_tier: Optional[str] = None
    in_scope: Optional[bool] = None
    annex: Optional[str] = Field(None, max_length=64)
    annex_iii_area: Optional[str] = Field(None, max_length=64)
    conformity_assessment: Optional[str] = Field(None, max_length=MAX_TEXT)
    # References the catalog's DECLARED statements cite.
    ai_literacy_reference: Optional[str] = Field(None, max_length=MAX_TEXT)
    instructions_for_use_reference: Optional[str] = Field(None, max_length=MAX_TEXT)
    qms_reference: Optional[str] = Field(None, max_length=MAX_TEXT)
    worker_information_reference: Optional[str] = Field(None, max_length=MAX_TEXT)
    deepfake_disclosure_approach: Optional[str] = Field(None, max_length=MAX_TEXT)
    gpai_documentation_reference: Optional[str] = Field(None, max_length=MAX_TEXT)
    synthetic_marking: Optional[str] = Field(None, max_length=MAX_TEXT)
    #: Art 86 — the procedure for explaining individual decisions.
    explanation_procedure_reference: Optional[str] = Field(None, max_length=MAX_TEXT)

    _annex = _one_of("annex", EU_ANNEXES)
    _annex_iii_area = _one_of("annex_iii_area", EU_ANNEX_III_AREAS)
    _conformity_assessment = _one_of("conformity_assessment", EU_CONFORMITY_ASSESSMENTS)

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v is not None and v not in EU_AI_ACT_ROLES:
            raise ValueError(f"role must be one of {sorted(EU_AI_ACT_ROLES)}")
        return v

    @field_validator("risk_tier")
    @classmethod
    def _tier(cls, v):
        if v is not None and v not in RISK_TIERS:
            raise ValueError(f"risk_tier must be one of {sorted(RISK_TIERS)}")
        return v


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= MAX_TEXT)


def _validate_block(framework_id: str, block: Any) -> Dict[str, Any]:
    if framework_id == EU_AI_ACT_ID:
        return EuAiActBlock.model_validate(block).model_dump(exclude_unset=True)
    if not isinstance(block, dict):
        raise ValueError(f"frameworks.{framework_id} must be an object")
    # Field shapes only: a stored block for a framework that is no longer
    # offered must still validate, or every unrelated save would fail.
    catalog = offering.any_catalog(framework_id)
    out: Dict[str, Any] = {}
    for key, value in block.items():
        if not _FIELD_KEY.match(str(key)):
            raise ValueError(f"frameworks.{framework_id}: field name {key!r} is not an identifier")
        if isinstance(value, list):
            if len(value) > MAX_LIST or not all(_scalar(x) for x in value):
                raise ValueError(f"frameworks.{framework_id}.{key} must be a short list of scalars")
        elif not _scalar(value):
            raise ValueError(f"frameworks.{framework_id}.{key} must be a scalar or a list of scalars")
        out[key] = value
    if "in_scope" in out and out["in_scope"] is not None and not isinstance(out["in_scope"], bool):
        raise ValueError(f"frameworks.{framework_id}.in_scope must be a boolean")
    if catalog is not None:
        role = out.get(catalog.role_field)
        if role is not None and catalog.roles and role not in catalog.roles:
            raise ValueError(
                f"frameworks.{framework_id}.{catalog.role_field} must be one of {sorted(catalog.roles)}"
            )
        cls_value = out.get(catalog.classification_field)
        if cls_value is not None and catalog.classifications and cls_value not in catalog.classifications:
            raise ValueError(
                f"frameworks.{framework_id}.{catalog.classification_field} must be one of "
                f"{sorted(catalog.classifications)}"
            )
    return out


def _first_error(exc: ValidationError, prefix: str) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{prefix}{'.' + loc if loc else ''}: {err.get('msg')}"


def validate(
    db: Session, core: Any, frameworks: Any, already_declared: Iterable[str] = ()
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Validated `(core, frameworks)`; 422 with the first problem.

    A framework that is not offered (not verified against its published text)
    cannot be ADDED: 409. One the profile already declares is
    kept, so saving an unrelated change does not fail."""
    if not isinstance(core, dict):
        raise HTTPException(status_code=422, detail="core must be an object")
    if not isinstance(frameworks, dict):
        raise HTTPException(status_code=422, detail="frameworks must be an object keyed by framework id")
    try:
        core_clean = CoreProfile.model_validate(core).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_first_error(exc, "core")) from exc
    known = {r.id for r in db.query(ComplianceFramework.id).all()}
    unknown = sorted(set(frameworks) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown framework ids {unknown}; registry has {sorted(known)}")
    regs = core_clean.get("external_registrations") or []
    bad_regs = sorted({r["framework_id"] for r in regs} - known)
    if bad_regs:
        raise HTTPException(status_code=422, detail=f"core.external_registrations names unknown frameworks {bad_regs}")
    kept = set(already_declared)
    for fid, block in frameworks.items():
        if block is None or fid in kept:
            continue
        known = offering.any_catalog(fid)
        reason = offering.unavailable_reason(known) if known is not None else None
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
    blocks: Dict[str, Any] = {}
    for fid, block in frameworks.items():
        if block is None:
            continue
        try:
            blocks[fid] = _validate_block(fid, block)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_first_error(exc, f"frameworks.{fid}")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return core_clean, blocks


def profile_view(system: ResourceGroup, row: Optional[AISystemComplianceProfile], tenant_id: str) -> Dict[str, Any]:
    """The profile as the console shows it, with the legacy EU columns read
    into the `eu-ai-act` block."""
    if row is None:
        return {
            "ai_system_id": system.id,
            "tenant_id": tenant_id,
            "core": {},
            "frameworks": {EU_AI_ACT_ID: {"role": system.eu_ai_act_role, "risk_tier": system.eu_ai_act_risk_tier}},
            "declaration_record_id": None,
            "sealed": False,
            "exists": False,
        }
    data = row.to_dict()
    frameworks = dict(data.get("frameworks") or {})
    if system.eu_ai_act_role is not None or system.eu_ai_act_risk_tier is not None:
        eu = dict(frameworks.get(EU_AI_ACT_ID) or {})
        eu["role"] = system.eu_ai_act_role
        eu["risk_tier"] = system.eu_ai_act_risk_tier
        frameworks[EU_AI_ACT_ID] = eu
    data["frameworks"] = frameworks
    data["exists"] = True
    data["sealed"] = bool(row.declaration_record_id)
    return data


def save(
    db: Session,
    system: ResourceGroup,
    tenant_id: str,
    core: Dict[str, Any],
    frameworks: Dict[str, Any],
) -> Tuple[AISystemComplianceProfile, Optional[Dict[str, Any]], Dict[str, Any], SealOutcome]:
    """Persist, write the EU block through to the group, commit, then seal
    the declaration and store its record id (committed again).

    Returns `(row, previous, group_changes, seal_outcome)`.
    """
    from app.services.compliance import evaluation
    from app.services.compliance.sealing_hooks import seal_declaration

    row = db.get(AISystemComplianceProfile, system.id)
    if row is not None and row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail=f"AI System {system.id!r} not found")
    previous = None if row is None else row.to_dict()
    if row is None:
        row = AISystemComplianceProfile(ai_system_id=system.id, tenant_id=tenant_id, core=core, frameworks=frameworks)
        db.add(row)
    else:
        row.core = core
        row.frameworks = frameworks
        flag_modified(row, "core")
        flag_modified(row, "frameworks")

    eu = frameworks.get(EU_AI_ACT_ID) or {}
    group_changes: Dict[str, Any] = {}
    if "role" in eu and system.eu_ai_act_role != eu["role"]:
        group_changes["eu_ai_act_role"] = {"from": system.eu_ai_act_role, "to": eu["role"]}
        system.eu_ai_act_role = eu["role"]
    if "risk_tier" in eu and system.eu_ai_act_risk_tier != eu["risk_tier"]:
        group_changes["eu_ai_act_risk_tier"] = {"from": system.eu_ai_act_risk_tier, "to": eu["risk_tier"]}
        system.eu_ai_act_risk_tier = eu["risk_tier"]
    # The previous declaration no longer describes this save.
    row.declaration_record_id = None
    db.commit()

    outcome = seal_declaration(db, tenant_id, system.id, {"core": core, "frameworks": frameworks})
    row.declaration_record_id = outcome.record_id
    db.commit()
    db.refresh(row)
    evaluation.clear_cache(tenant_id)
    return row, previous, group_changes, outcome


__all__ = [
    "CoreProfile",
    "EU_AI_ACT_ID",
    "EuAiActBlock",
    "OPERATOR_ROLES",
    "profile_view",
    "save",
    "validate",
]
