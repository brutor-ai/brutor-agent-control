# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Drift resolution API models

Closing a drift finding takes a kind; each kind has one effect the
detector honours (a baseline epoch or an incident window). Incident windows
are also declared directly. Every field the service layer produces is
declared here: Pydantic strips undeclared fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

from app.api.v1.endpoints.admin._compliance_http import SealStatus

#: The kinds a human chooses when closing a finding (`auto_cleared` is the
#: control plane's own). Pinned against the wire constants.
HumanResolutionKind = Literal["accepted_change", "cause_fixed", "false_positive"]
ResolutionKind = Literal["accepted_change", "cause_fixed", "false_positive", "auto_cleared"]
IncidentCauseCategory = Literal["provider_outage", "platform_incident", "maintenance", "other"]
IncidentResourceType = Literal["model", "provider", "mcp_server", "skill", "knowledge_base", "agent"]
EpochKind = Literal["accepted_change", "false_positive", "measurement_change", "catalogue_change"]


class CloseDriftRequest(BaseModel):
    """Close a drift finding. `kind` and `note` are required — the platform
    decides nothing silently."""

    kind: HumanResolutionKind = Field(
        ...,
        description=(
            "accepted_change: the shift is the new normal — a baseline epoch starts at the "
            "finding's change point (else detection time) and the metric re-learns from "
            "there. cause_fixed: something was wrong and is fixed — an incident window "
            "[change point, now] (bounds editable) is excluded from judgement. "
            "false_positive: the detector was wrong — the evidence up to detection is not "
            "read again for this metric (an epoch at detection time)."
        ),
    )
    note: str = Field(..., min_length=1, max_length=2000)
    window_starts_at: Optional[datetime] = Field(
        None, description="cause_fixed only. Default: the change point, else detected_at."
    )
    window_ends_at: Optional[datetime] = Field(
        None, description="cause_fixed only (exclusive). Default: now."
    )
    cause_category: Optional[IncidentCauseCategory] = Field(
        None, description="cause_fixed only. Default: other."
    )
    tenant_wide: bool = Field(
        False,
        description=(
            "cause_fixed only: the window covers every AI System in the tenant (a provider "
            "outage), not just the finding's."
        ),
    )
    incident_window_id: Optional[str] = Field(
        None,
        description=(
            "cause_fixed only, and instead of the window fields above: attach this finding "
            "to an EXISTING incident window of the tenant whose scope covers the finding's "
            "AI System (grouped findings of one incident: the first declares the window, "
            "the rest attach). 422 if it is not found or does not cover the system."
        ),
    )


class BaselineEpochResponse(BaseModel):
    id: str
    ai_system_id: str
    metric_key: str
    basis: str
    caller_system_id: str = Field(
        ..., description="'all' covers every caller of the metric"
    )
    bucket: str = Field(
        "all",
        description=(
            "'all' covers every bucket of the metric; else one outcome bucket "
            "(a full outcome id, catalogue_change epochs)"
        ),
    )
    starts_at: Optional[str] = None
    kind: EpochKind
    drift_event_id: Optional[str] = None
    measurement_change_id: Optional[str] = Field(
        None,
        description="measurement_change epochs: the platform registry entry",
    )
    reason: Optional[str] = None
    declared_by: Optional[str] = None
    declared_at: Optional[str] = None
    record_id: Optional[str] = None
    sealed_at: Optional[str] = None


class BaselineEpochListResponse(BaseModel):
    epochs: List[BaselineEpochResponse]
    total: int


class IncidentWindowResponse(BaseModel):
    id: str
    ai_system_id: Optional[str] = Field(None, description="NULL = every AI System in the tenant")
    ai_system_name: Optional[str] = None
    tenant_wide: bool = False
    resource_type: Optional[IncidentResourceType] = None
    resource_id: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = Field(None, description="Exclusive")
    cause_category: IncidentCauseCategory
    reason: str
    drift_event_id: Optional[str] = None
    declared_by: Optional[str] = None
    declared_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    record_id: Optional[str] = None
    sealed_at: Optional[str] = None
    drift_event_ids: List[str] = Field(
        default_factory=list,
        description="The cause_fixed findings this window closed: the declaring one first, then those attached",
    )
    runs_excluded: int = Field(
        0,
        description=(
            "Runs whose start lies in the window (of the window's system, or of every "
            "system when tenant-wide). Excluded from judgement, kept as evidence."
        ),
    )


class IncidentWindowListResponse(BaseModel):
    windows: List[IncidentWindowResponse]
    total: int


class IncidentWindowCreate(BaseModel):
    ai_system_id: Optional[str] = Field(
        None,
        description=(
            "Tenant route only: the AI System the window covers; omit for every system in the "
            "tenant. The per-system route sets it from the path."
        ),
    )
    starts_at: datetime
    ends_at: datetime = Field(..., description="Exclusive; after starts_at")
    cause_category: IncidentCauseCategory
    reason: str = Field(..., min_length=1, max_length=2000)
    resource_type: Optional[IncidentResourceType] = None
    resource_id: Optional[str] = Field(None, max_length=255)


class IncidentWindowUpdate(BaseModel):
    """Every edit is a new sealed declaration. The scope (system /
    tenant-wide) is not editable: withdraw and declare again."""

    model_config = ConfigDict(extra="forbid")

    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    cause_category: Optional[IncidentCauseCategory] = None
    reason: Optional[str] = Field(None, min_length=1, max_length=2000)
    resource_type: Optional[IncidentResourceType] = None
    resource_id: Optional[str] = Field(None, max_length=255)


class IncidentWindowWriteResponse(IncidentWindowResponse):
    seal: SealStatus


class IncidentWindowDeleteResponse(BaseModel):
    id: str
    deleted: bool = True
    seal: SealStatus


__all__ = [
    "BaselineEpochListResponse",
    "BaselineEpochResponse",
    "CloseDriftRequest",
    "EpochKind",
    "HumanResolutionKind",
    "IncidentCauseCategory",
    "IncidentResourceType",
    "IncidentWindowCreate",
    "IncidentWindowDeleteResponse",
    "IncidentWindowListResponse",
    "IncidentWindowResponse",
    "IncidentWindowUpdate",
    "IncidentWindowWriteResponse",
    "ResolutionKind",
]
