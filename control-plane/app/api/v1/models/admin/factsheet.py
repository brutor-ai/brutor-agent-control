# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic request/response models for the AI Asset Factsheet admin API.

The live factsheet response is built from the dossier dict that
`FactsheetService.build_factsheet` returns via `FactsheetResponse(**dossier)`,
so the top-level field names MUST stay in lockstep with the dossier keys
(the Pydantic strip-undeclared-fields gotcha). Nested sections are typed
sub-models where the shape is stable; the `asset` and `config` sections
are intentionally loose dicts (config shape varies per asset_kind).
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Nested sections
# =============================================================================


class FactsheetUsage(BaseModel):
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    success_count: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    avg_duration_ms: float = 0.0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    # Observed (imported) split — usage reconciled from vendor analytics via
    # Traffic Data Import. Included in the totals above; broken out here so
    # the governed/observed planes stay distinguishable per asset.
    observed_requests: int = 0
    observed_cost_usd: float = 0.0


class FactsheetGuardrails(BaseModel):
    guardrail_blocked: int = 0
    pii_detected: int = 0
    argument_policy_denied: int = 0


class FactsheetCompliance(BaseModel):
    # Per-framework implicated row counts, asset-scoped.
    frameworks: Dict[str, int] = Field(default_factory=dict)


class FactsheetResidency(BaseModel):
    regions_routed_to: List[str] = Field(default_factory=list)
    residency_class_counts: Dict[str, int] = Field(default_factory=dict)


class FactsheetResourceGroupRef(BaseModel):
    id: str
    name: Optional[str] = None


class FactsheetAccess(BaseModel):
    resource_groups: List[FactsheetResourceGroupRef] = Field(default_factory=list)


class FactsheetMeta(BaseModel):
    generated_at: str
    period_hours: int
    window_start: Optional[str] = None
    window_end: str


# =============================================================================
# Response models
# =============================================================================


class FactsheetResponse(BaseModel):
    """The live, on-demand factsheet dossier for an AI asset.

    Top-level keys mirror `FactsheetService.build_factsheet`'s dict.
    """

    asset: Dict[str, Any]
    config: Dict[str, Any]
    usage: FactsheetUsage
    guardrails: FactsheetGuardrails
    compliance: FactsheetCompliance
    residency: FactsheetResidency
    access: FactsheetAccess
    meta: FactsheetMeta


class FactsheetSnapshotResponse(BaseModel):
    """A persisted, point-in-time factsheet snapshot row.

    Field names mirror `AiAssetFactsheet.to_dict()`.
    """

    id: str
    tenant_id: str
    asset_id: str
    period_hours: int
    snapshot_json: Dict[str, Any]
    trigger: Optional[str] = None
    created_at: Optional[str] = None


class FactsheetSnapshotListResponse(BaseModel):
    snapshots: List[FactsheetSnapshotResponse]
    total: int
