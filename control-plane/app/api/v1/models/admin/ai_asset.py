# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for the AI Asset Registry admin API.

Response shapes are built from `AiAsset.to_dict()` via `Model(**row.to_dict())`,
so the response field names MUST stay in lockstep with the to_dict keys
(the Pydantic strip-undeclared-fields gotcha).
"""

from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.services import release_scope

from app.database.models.ai_asset import (
    ASSET_KINDS,
    ENFORCEMENT_MODES,
    LIFECYCLE_STAGES,
    RISK_TIERS,
    STATUSES,
)


# Sorted literals for OpenAPI docs — kept derived from the model
# frozensets so they cannot drift from the @validates vocabulary.
_ASSET_KINDS = sorted(ASSET_KINDS)
_LIFECYCLE_STAGES = sorted(LIFECYCLE_STAGES)
_RISK_TIERS = sorted(RISK_TIERS)
_ENFORCEMENT_MODES = sorted(ENFORCEMENT_MODES)
_STATUSES = sorted(STATUSES)


# =============================================================================
# Request models
# =============================================================================


class AiAssetCreateRequest(BaseModel):
    """Create a manual AI asset.

    `asset_kind` + `ref_id` are required; everything else is optional
    governance metadata. The service validates the vocabularies (also
    enforced at the model layer) and rejects a duplicate
    (tenant, asset_kind, ref_id).
    """

    asset_kind: str = Field(
        ...,
        description=f"Kind of underlying resource; one of {_ASSET_KINDS}",
        max_length=32,
    )
    ref_id: str = Field(
        ...,
        description="Id of the underlying config row this asset describes",
        min_length=1,
        max_length=255,
    )
    display_name: Optional[str] = Field(None, max_length=255)
    owner: Optional[str] = Field(None, max_length=255)
    lifecycle_stage: Optional[str] = Field(
        None,
        description=f"Governance lifecycle; one of {_LIFECYCLE_STAGES}",
        max_length=32,
    )
    risk_tier: Optional[str] = Field(
        None,
        description=f"EU AI Act-aligned risk tier; one of {_RISK_TIERS}",
        max_length=32,
    )
    intended_use: Optional[str] = None
    gpai_classification: Optional[str] = Field(
        None,
        description="GPAI track (llm_model assets): gpai | gpai_systemic_risk",
        max_length=24,
    )
    operated_by: Optional[str] = Field(
        None,
        description="Who operates this constituent: self | third_party",
        max_length=16,
    )
    enforcement_mode: Optional[str] = Field(
        None,
        description=(
            "Governance enforcement of the asset. Only 'off' is available in this release: the "
            "gateway does not read the asset register, so 'warn' and 'enforce' are refused."
        ),
        max_length=16,
    )
    tags: Optional[List[str]] = None

    @field_validator("enforcement_mode")
    @classmethod
    def _enforcement_in_this_release(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value != "off":
            raise ValueError(release_scope.ASSET_ENFORCEMENT)
        return value


class AiAssetUpdateRequest(BaseModel):
    """Partial update — every field is optional and applied only when set.

    Only governance metadata is mutable; `asset_kind` / `ref_id` /
    `source` are fixed at create/discovery time.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(None, max_length=255)
    owner: Optional[str] = Field(None, max_length=255)
    lifecycle_stage: Optional[str] = Field(None, max_length=32)
    risk_tier: Optional[str] = Field(None, max_length=32)
    intended_use: Optional[str] = None
    gpai_classification: Optional[str] = Field(None, max_length=24)
    operated_by: Optional[str] = Field(None, max_length=16)
    enforcement_mode: Optional[str] = Field(
        None,
        description=(
            "Governance enforcement of the asset. Only 'off' is available in this release: the "
            "gateway does not read the asset register, so 'warn' and 'enforce' are refused."
        ),
        max_length=16,
    )
    tags: Optional[List[str]] = None
    status: Optional[str] = Field(
        None,
        description=f"Review status; one of {_STATUSES}",
        max_length=32,
    )

    @field_validator("enforcement_mode")
    @classmethod
    def _enforcement_in_this_release(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value != "off":
            raise ValueError(release_scope.ASSET_ENFORCEMENT)
        return value


# =============================================================================
# Response models
# =============================================================================


class AiAssetResponse(BaseModel):
    """Full AI asset row. Field names mirror `AiAsset.to_dict()`."""

    id: str
    tenant_id: str
    asset_kind: str
    ref_id: str
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    source_enabled: Optional[bool] = None
    observed: bool = False
    owner: Optional[str] = None
    lifecycle_stage: str
    risk_tier: Optional[str] = None
    intended_use: Optional[str] = None
    gpai_classification: Optional[str] = None
    operated_by: Optional[str] = None
    enforcement_mode: str = Field(..., description="'off': the gateway does not read the asset register in this release.")
    tags: List[str] = Field(default_factory=list)
    status: str
    source: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # ── Derived, never stored ──
    # Annex III classifies by SYSTEM purpose, so a component asset has
    # no risk class of its own — it inherits exposure from the AI
    # Systems that bind it. `owning_systems` lists those (direct
    # bindings); `derived_risk_tier` is the worst tier among them.
    owning_systems: List["OwningSystemRef"] = Field(default_factory=list)
    derived_risk_tier: Optional[str] = None


class OwningSystemRef(BaseModel):
    """An AI System that binds this asset (the register's edge view)."""

    id: str
    name: str
    eu_ai_act_risk_tier: Optional[str] = None


class AiAssetListResponse(BaseModel):
    assets: List[AiAssetResponse]
    total: int


class AssetSyncResponse(BaseModel):
    """Outcome of an auto-discovery sync run."""

    created: int
    updated: int
    orphaned: int
    total: int
