# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI regulatory obligations — a dated catalog, computed per system

The register models "obligation X applies to system Y **from date Z**",
never "system Y is compliant: true". A boolean dies at the next omnibus;
an applicability date survives it — Regulation (EU) 2026/1744 moved four
of the EU AI Act's dates after everyone had already planned around them.

The catalog types and the catalogs themselves moved to
`app/services/compliance/catalogs/`; this module keeps the
catalog names importable and owns the DB side: collecting one system's
declared facts (`system_context`) for a given framework.

Undeclared facts yield `undetermined` — an undeclared role cannot rule an
obligation out, and the honest answer is "declare it", not a guess.
Nothing here is stored: applicability derives from the system's current
declarations at read time, so it can never go stale against them.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.ai_asset import (
    ASSET_KIND_LLM_MODEL,
    GPAI,
    GPAI_SYSTEMIC_RISK,
    AiAsset,
)
from app.database.models.ai_system_compliance_profile import (
    CORE_BOOLEAN_FIELDS,
    AISystemComplianceProfile,
)
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
)
from app.services.compliance.catalogs.base import (
    EvidenceBasis,
    EvidenceSpec,
    Framework,
    Obligation,
    STATUS_APPLIES,
    STATUS_NOT_APPLICABLE,
    STATUS_UNDETERMINED,
    SystemContext,
    evaluate,
)
from app.services.compliance.catalogs.eu_ai_act import (
    DEPLOYER_SIDE,
    FRAMEWORK as EU_AI_ACT,
    PROVIDER_SIDE,
)



def load_profile(db: Session, group: ResourceGroup) -> Optional[AISystemComplianceProfile]:
    row = db.get(AISystemComplianceProfile, group.id)
    if row is not None and row.tenant_id != group.tenant_id:
        return None
    return row


def evaluated_tier(
    framework: Framework, declared: Optional[str], sid: str, tier_facts: Optional[Any]
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """`(tier the registry evaluates the system at, the systems that raised
    it)`. Under a framework with `composite_classification` that is the
    EFFECTIVE tier — max(declared, the tier of every system
    whose decision this one contributes to); otherwise the declared tier.

    ONE rule for obligations, statements and retention floors.
    """
    if not framework.composite_classification or tier_facts is None:
        return declared, []
    from app.services.ai_system_composition import max_tier, tier_rank

    effective = max_tier(declared, tier_facts.effective_tier(sid))
    if tier_rank(effective) <= tier_rank(declared):
        return declared, []
    return effective, tier_facts.sources(sid)


def system_context(
    db: Session,
    group: ResourceGroup,
    framework: Optional[Framework] = None,
    profile: Any = False,
    tier_facts: Any = None,
) -> SystemContext:
    """Collect the system's declared facts for `framework` (default EU AI
    Act), including GPAI exposure via its directly bound models.

    The EU AI Act classification and role come from the group's
    `eu_ai_act_*` columns (the profile save writes through to them);
    other frameworks read `profile.frameworks[<id>][role_field /
    classification_field]`. Neutral-core flags come from `profile.core`.

    Under a framework with `composite_classification` the classification is
    the system's EFFECTIVE tier; `tier_facts` (the tenant's
    `load_tier_facts`) is loaded here when the caller has none to share.
    """
    framework = framework or EU_AI_ACT
    if profile is False:
        profile = load_profile(db, group)
    rows = db.execute(
        select(AiAsset.display_name, AiAsset.ref_id, AiAsset.gpai_classification)
        .join(
            resource_group_llm_models,
            resource_group_llm_models.c.llm_model_id == AiAsset.ref_id,
        )
        .where(
            resource_group_llm_models.c.resource_group_id == group.id,
            AiAsset.tenant_id == group.tenant_id,
            AiAsset.asset_kind == ASSET_KIND_LLM_MODEL,
            AiAsset.gpai_classification.isnot(None),
        )
    ).all()
    gpai, systemic = [], []
    for display_name, ref_id, cls in rows:
        name = display_name or ref_id
        if cls == GPAI_SYSTEMIC_RISK:
            systemic.append(name)
        elif cls == GPAI:
            gpai.append(name)

    core = dict(profile.core or {}) if profile is not None else {}
    blocks = dict(profile.frameworks or {}) if profile is not None else {}
    block = blocks.get(framework.id) or {}
    if framework.id == EU_AI_ACT.id:
        risk_tier = group.eu_ai_act_risk_tier or block.get(framework.classification_field)
        role = group.eu_ai_act_role or block.get(framework.role_field)
    else:
        risk_tier = block.get(framework.classification_field)
        role = block.get(framework.role_field)
    flags = {
        name: (core.get(name) if isinstance(core.get(name), bool) else None)
        for name in CORE_BOOLEAN_FIELDS
    }
    declared = risk_tier if isinstance(risk_tier, str) else None
    if framework.composite_classification and tier_facts is None:
        from app.services.ai_system_composition import load_tier_facts

        tier_facts = load_tier_facts(db, group.tenant_id)
    evaluated, sources = evaluated_tier(framework, declared, group.id, tier_facts)
    return SystemContext(
        risk_tier=evaluated,
        role=role if isinstance(role, str) else None,
        gpai_models=gpai,
        gpai_systemic_models=systemic,
        flags=flags,
        risk_tier_declared=declared,
        risk_tier_sources=sources,
    )


def evaluate_system(
    db: Session, framework: Framework, group: ResourceGroup, *, today: date,
    tier_facts: Any = None,
) -> List[Dict[str, object]]:
    ctx = system_context(db, group, framework, tier_facts=tier_facts)
    version = framework.version_on(today)
    catalog_version = version.catalog_version if version else None
    return [
        evaluate(o, ctx, today=today, catalog_version=catalog_version)
        for o in framework.obligations_in(catalog_version)
    ]


__all__ = [
    "evaluated_tier",
    "DEPLOYER_SIDE",
    "EU_AI_ACT",
    "EvidenceBasis",
    "EvidenceSpec",
    "Framework",
    "Obligation",
    "PROVIDER_SIDE",
    "STATUS_APPLIES",
    "STATUS_NOT_APPLICABLE",
    "STATUS_UNDETERMINED",
    "SystemContext",
    "evaluate",
    "evaluate_system",
    "load_profile",
    "system_context",
]
