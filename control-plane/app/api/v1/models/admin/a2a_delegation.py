# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic response shapes for the A2A delegation-graph admin API.

Three response shapes, one per endpoint:

  * `A2ADelegationGraphSummary`  — one entry per root_task_id in the
     list endpoint; aggregates all steps of a graph into a single row.
  * `A2ADelegationGraphDetail`   — the full tree for one root, with
     per-step nodes ordered by (depth, timestamp).
  * `A2ADelegationGraphStats`    — aggregate counts for charts / KPIs.

We don't re-use the step row shape across summary vs detail because the
summary collapses siblings into aggregates and the detail preserves
them — mixing the two in one Pydantic model would force the UI to
branch on a discriminator, which is more fragile than two flat shapes.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# List — summary per root_task_id
# ---------------------------------------------------------------------------


class A2ADelegationGraphSystem(BaseModel):
    """An AI System the graph's run involved."""

    id: str
    name: Optional[str] = None
    role: Literal["root", "segment"] = Field(
        ..., description="root: the run's attributed system; segment: a system it passed through"
    )


class A2ADelegationGraphSummary(BaseModel):
    """One row per delegation graph (= one root_task_id)."""

    root_task_id: str
    tenant_id: str
    started_at: datetime
    ended_at: datetime
    duration_ms: Optional[float] = Field(
        default=None,
        description=(
            "ended_at - started_at, redundant with the two timestamps "
            "but convenient for sort-by-duration in the UI."
        ),
    )
    total_steps: int
    max_depth: int
    inbound_steps: int
    outbound_steps: int
    success_steps: int
    failure_steps: int
    # `overall_success=True` iff every step in the graph was success.
    # An all-but-last-step-succeeds failure pattern is very common so
    # we surface it explicitly.
    overall_success: bool
    # Root-step metadata — the entry point into this graph.
    root_endpoint: Optional[str] = None
    root_card_id: Optional[str] = None
    root_card_name: Optional[str] = None
    root_actor_agent_id: Optional[str] = None
    root_actor_agent_name: Optional[str] = None
    root_correlation_id: Optional[str] = None
    # The AI Systems involved (run ledger + run segments), so the
    # page links each graph to its systems. Empty when the run is not (yet)
    # finalised in the ledger.
    systems: List[A2ADelegationGraphSystem] = Field(default_factory=list)


class A2ADelegationGraphList(BaseModel):
    graphs: List[A2ADelegationGraphSummary]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Detail — full tree of steps for one graph
# ---------------------------------------------------------------------------


class A2ADelegationStep(BaseModel):
    """One step (one proxy_logs row) inside a delegation graph."""

    # Step identity — the node id in the tree.
    current_task_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    depth: int
    # Direction / role
    direction: Optional[str] = Field(
        default=None,
        description="'inbound' or 'outbound' — which side of this step the gateway played",
    )
    # Link into the proxy_logs row itself.
    proxy_log_id: str
    # Request metadata the operator cares about.
    timestamp: datetime
    duration_ms: Optional[float] = None
    http_status_code: Optional[int] = None
    success: bool
    error_message: Optional[str] = None
    # A2A semantics
    card_id: Optional[str] = None
    endpoint: Optional[str] = None
    target_url: Optional[str] = None
    correlation_id: Optional[str] = None
    # Who → whom. The caller is the acting agent principal (actor_agent_id);
    # the callee is the target card (card_id). Names are resolved from the
    # agent-identity / agent-card registries so the graph reads in plain terms.
    actor_agent_id: Optional[str] = None
    actor_agent_name: Optional[str] = None
    subject_id: Optional[str] = None
    subject_kind: Optional[str] = None
    card_name: Optional[str] = None


class A2ADelegationGraphDetail(BaseModel):
    root_task_id: str
    tenant_id: str
    steps: List[A2ADelegationStep]
    total_steps: int
    max_depth: int


# ---------------------------------------------------------------------------
# Stats — aggregate counts over a window
# ---------------------------------------------------------------------------


class A2ADelegationTopCard(BaseModel):
    card_id: str
    count: int


class A2ADelegationStats(BaseModel):
    tenant_id: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    total_graphs: int
    total_steps: int
    inbound_steps: int
    outbound_steps: int
    success_steps: int
    failure_steps: int
    success_rate: Optional[float] = Field(
        default=None,
        description="success_steps / total_steps; null when total_steps==0",
    )
    avg_steps_per_graph: Optional[float] = None
    max_depth_observed: Optional[int] = None
    avg_duration_ms: Optional[float] = None
    p50_duration_ms: Optional[float] = None
    p95_duration_ms: Optional[float] = None
    top_cards: List[A2ADelegationTopCard] = Field(
        default_factory=list,
        description="Most-invoked cards in this window (id + count)",
    )
