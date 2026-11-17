# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance framework Pydantic shapes.

Three boundary models:

  * `ComplianceProfile` — JSONB stored on `tenants.compliance_profile`.
    Declares which regimes apply to a tenant. The tagging engine
    reads this on every call; an empty `frameworks` list short-
    circuits the engine (no tags written).

  * `ResourceGroupComplianceFields` — patch payload for the RG
    admin endpoint to set `eu_ai_act_risk_tier` + `phi_resource`
    without re-shipping every other RG field.

  * `ComplianceTagsResponse` — read-side shape for the tagged
    audit row's compliance_tags array. The runtime stamps
    interned strings; we validate the array shape here for API
    consumers.

The five framework keys are fixed (see `FRAMEWORK_KEYS`) so a
typo in the admin UI doesn't ship an unrecognized regime.
Adding a regime is a deliberate three-place change: this list,
the tagging engine, and the export-endpoints registry.

Risk-tier values are pinned to the EU AI Act's four categories
(`minimal`, `limited`, `high`, `unacceptable`) and mirror the
CHECK constraint on `resource_groups`.
"""

from __future__ import annotations

from typing import List, Optional, Set

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.database.models.compliance_framework import registry_tag_keys


# The tagging framework keys — read from the framework registry
# (`compliance_frameworks.tag_key`), never re-declared here:
# gdpr_art_30, soc2, hipaa, eu_ai_act, iso_42001. Adding a tagging regime
# is a registry row plus its derivation in the tagging engines (Python and
# Rust, pinned by tests/test_compliance_python_rust_parity.py).
FRAMEWORK_KEYS: Set[str] = set(registry_tag_keys().values())


EU_AI_ACT_RISK_TIERS: Set[str] = {
    "minimal",
    "limited",
    "high",
    "unacceptable",
}


class ComplianceProfile(BaseModel):
    """Per-tenant compliance regime declaration.

    Stored at `tenants.compliance_profile` (JSONB). An empty
    `frameworks` list — the default — disables compliance tagging
    entirely: the engine short-circuits and `proxy_logs.
    compliance_tags` stays NULL for every row from this tenant.
    """

    model_config = ConfigDict(extra="forbid")

    frameworks: List[str] = Field(
        default_factory=list,
        description=(
            "Frameworks this tenant must comply with. Choose from "
            "gdpr_art_30, soc2, hipaa, eu_ai_act, iso_42001. Empty "
            "list = no compliance tagging."
        ),
    )

    @field_validator("frameworks")
    @classmethod
    def _validate_frameworks(cls, v: List[str]) -> List[str]:
        # Validate each entry against the pinned set, dedupe
        # preserving order. Unknown frameworks fail loudly at API
        # save time so operators see the typo rather than silently
        # untagged rows.
        seen: Set[str] = set()
        out: List[str] = []
        for fw in v:
            if not isinstance(fw, str):
                raise ValueError(f"framework entries must be strings, got {type(fw).__name__}")
            fw_clean = fw.strip().lower()
            if not fw_clean:
                continue
            if fw_clean not in FRAMEWORK_KEYS:
                raise ValueError(
                    f"unknown framework {fw!r}; expected one of "
                    f"{sorted(FRAMEWORK_KEYS)}"
                )
            if fw_clean not in seen:
                seen.add(fw_clean)
                out.append(fw_clean)
        return out


class ResourceGroupComplianceFields(BaseModel):
    """PATCH-shape for the RG compliance fields.

    Both fields are explicitly Optional because the admin UI
    submits partial patches — `eu_ai_act_risk_tier=None`
    intentionally clears the tier, but omitting the field leaves
    it unchanged. The endpoint distinguishes via
    `model_dump(exclude_unset=True)`.
    """

    eu_ai_act_risk_tier: Optional[str] = Field(
        default=None,
        description=(
            "EU AI Act risk tier for this RG: minimal | limited | "
            "high | unacceptable. NULL to clear."
        ),
    )
    phi_resource: Optional[bool] = Field(
        default=None,
        description=(
            "True if this RG handles Protected Health Information. "
            "Only meaningful when tenant lists hipaa in its profile."
        ),
    )

    @field_validator("eu_ai_act_risk_tier")
    @classmethod
    def _validate_risk_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_clean = v.strip().lower()
        if not v_clean:
            return None
        if v_clean not in EU_AI_ACT_RISK_TIERS:
            raise ValueError(
                f"eu_ai_act_risk_tier must be one of "
                f"{sorted(EU_AI_ACT_RISK_TIERS)}, got {v!r}"
            )
        return v_clean


class ComplianceTagsResponse(BaseModel):
    """Read-side shape for the per-row compliance_tags array.

    The runtime stamps interned tags (e.g. `gdpr_art_30`,
    `soc2.cc6.1`, `hipaa.phi`). This model is used by audit-export
    endpoints to normalise NULL → empty list at the API boundary
    without losing the in-DB distinction (NULL = engine never
    ran, [] = engine ran but found no applicable regime — these
    are different audit states)."""

    tags: List[str] = Field(
        default_factory=list,
        description="Interned compliance regime tags fired for this row.",
    )
    evaluated: bool = Field(
        default=True,
        description=(
            "False when the source row's compliance_tags was NULL "
            "(tenant has no compliance profile). True when the "
            "engine ran, even if it produced an empty tag list."
        ),
    )

    @classmethod
    def from_row_value(cls, raw: Optional[List[str]]) -> "ComplianceTagsResponse":
        """Build from the DB value. NULL → evaluated=False; a list
        (even empty) → evaluated=True."""
        if raw is None:
            return cls(tags=[], evaluated=False)
        return cls(tags=list(raw), evaluated=True)
