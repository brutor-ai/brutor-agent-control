# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Wire models for the System Operations Console **Caches** sub-section.

These models are kept in a separate file from `system_ops.py` so each
phase can ship its incremental schema additions without re-touching
the same 600-line file from multiple slices in parallel — the layer
gets re-exported from `system_ops.py` itself for callers that only
want the umbrella import.

`CacheFlushRequest` lives in `system_ops.py` with the channel
registry; this file ONLY adds the read-side `CacheStatsResponse` shape.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.models.admin.system_ops import CacheLayer, CacheScope


class CacheLayerStats(BaseModel):
    """One card in the Caches page grid.

    All counters are best-effort — see
    `app/services/system_ops/caches.py` for the per-layer sourcing
    notes. The wire contract pads zero-valued layers with a
    truthful `detail` string so the UI can render a "stats not
    available" hint rather than misleading numbers.
    """

    model_config = ConfigDict(extra="forbid")

    layer: CacheLayer
    hit_count: int = Field(
        ge=0,
        description=(
            "Hit count over the look-back window. Semantic layer reads "
            "from proxy_logs.llm_cache_hit; other layers are 0 until the "
            "gateway-core fan-out round-trip ships."
        ),
    )
    miss_count: int = Field(ge=0)
    hit_rate: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Fraction in [0, 1]. UI renders as a percentage; the "
            "narrower wire shape avoids a stringly-typed contract."
        ),
    )
    item_count_approx: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Approximate entry count. NULL when the control plane "
            "can't see the in-process moka store (everything except "
            "semantic, today)."
        ),
    )
    last_flushed_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO-8601 timestamp of the most recent successful flush "
            "from `system_operation_log` for this tenant + layer."
        ),
    )
    scopes_available: List[CacheScope] = Field(
        default_factory=list,
        description=(
            "Which scope kinds the flush button can target for this "
            "layer. Different layers expose different "
            "invalidation granularities."
        ),
    )
    detail: Optional[str] = Field(
        default=None,
        description=(
            "Operator-facing limitation hint. Set when counters are "
            "zero because the data source doesn't expose them yet."
        ),
    )


class CacheStatsResponse(BaseModel):
    """GET /v1/admin/system/caches body."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    generated_at: str = Field(description="ISO-8601 timestamp of the response.")
    layers: List[CacheLayerStats] = Field(
        description=(
            "One entry per cache layer, in this order: "
            "semantic, policy, group, tool, response."
        ),
    )
