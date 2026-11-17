# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the System Operations Console — Connections tab.

Lives in a slice-specific module so we don't keep growing
`system_ops.py` past readable. The endpoint imports + re-exports
these alongside the other system_ops models, but the source of
truth is here.

Two response shapes + one request shape:

  * `ConnectionPool` — one entry per pool (Postgres, Redis, Qdrant,
    per-LLM-provider). The shape is deliberately wide enough to hold
    every flavour: Postgres fills the size / checked_in / checked_out
    / overflow / saturation columns; Redis fills none today (honest
    'unknown'); Qdrant uses `client_count` + `most_recent_latency_ms`;
    LLM providers list with everything null and rely on `detail` to
    point operators at the Rust proxy's metrics.

  * `ConnectionListResponse` — wraps the entries list with a
    generated_at timestamp the UI uses for staleness display.

  * `PoolRecycleRequest` — POST body for the recycle endpoint. Only
    `postgres` and `redis` are accepted at this layer; any other
    value is a 422 before the service call.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Discriminator for which pool kinds the recycle endpoint accepts.
# Postgres + Redis are the only two the CONTROL PLANE owns — the
# Rust proxy's pools are out of scope for this endpoint.
RecyclablePoolKind = Literal["postgres", "redis"]


class ConnectionPool(BaseModel):
    """One row on the Connections page.

    Fields are nullable across the board because no single pool type
    fills every slot:
      * `size` / `checked_in` / `checked_out` / `overflow` — Postgres only.
      * `client_count`                                      — Qdrant + Redis.
      * `most_recent_latency_ms`                            — Qdrant (when an alert exists).
      * `saturation`                                        — derived; null when size unavailable.

    `detail` is the operator-facing one-line status — always populated
    so a card never renders blank.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description=(
            "Pool identifier. 'postgres', 'redis', 'qdrant', or "
            "'llm_provider:<provider_id>' for LLM upstream entries. "
            "The UI dispatches recycle buttons on the literal "
            "'postgres' / 'redis' values."
        ),
    )
    label: str = Field(
        description="Human-readable card title.",
    )

    # Pool-size telemetry — Postgres fills these, others NULL.
    size: Optional[int] = Field(
        default=None,
        ge=0,
        description="Configured pool size. NULL when the pool kind has no size concept.",
    )
    checked_in: Optional[int] = Field(default=None, ge=0)
    checked_out: Optional[int] = Field(default=None, ge=0)
    overflow: Optional[int] = Field(
        default=None,
        description=(
            "Overflow slots currently in use beyond `size`. NULL when "
            "the pool kind doesn't support overflow."
        ),
    )

    # Derived saturation = checked_out / size. NULL when size is NULL
    # or zero (e.g. Redis / Qdrant entries).
    saturation: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Fraction of pool currently in use. Values >1 mean the pool "
            "is overflowing — the UI renders that as a full red bar plus "
            "an 'overflowed' badge."
        ),
    )

    # Used by Qdrant / Redis ('how many open clients right now').
    client_count: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Open clients right now — used by per-call pool kinds that "
            "don't expose size/checked_out telemetry."
        ),
    )

    recyclable: bool = Field(
        description=(
            "True when the recycle button should be enabled for this "
            "pool. Postgres is the only kind that actually disposes "
            "today; Redis is recyclable=False until the persistent "
            "client lands."
        ),
    )

    most_recent_latency_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Most-recent observed round-trip in ms — read from "
            "system_alerts.context for Qdrant. NULL when no recent "
            "data point is available."
        ),
    )

    detail: str = Field(
        description=(
            "One-line operator-facing status — always populated so "
            "every card has something to show."
        ),
    )
    observed_at: str = Field(
        description="ISO-8601 timestamp the entry was built.",
    )


class ConnectionListResponse(BaseModel):
    """GET /v1/admin/system/connections response body."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Tenant the listing was scoped to. NULL when the caller "
            "is a system admin requesting an unscoped view."
        ),
    )
    generated_at: str = Field(
        description="ISO-8601 timestamp of the response build.",
    )
    pools: List[ConnectionPool]


class PoolRecycleRequest(BaseModel):
    """POST /v1/admin/system/connections/recycle body.

    The endpoint allows only `postgres` and `redis` — the only two
    pools the control plane actually owns. Sending anything else is a
    Pydantic 422 *before* we audit, so the audit row only fires on a
    valid attempt (typos and misuses don't pollute the trail).
    """

    model_config = ConfigDict(extra="forbid")

    kind: RecyclablePoolKind = Field(
        description=(
            "Which pool to dispose. 'postgres' calls "
            "engine.dispose(); 'redis' is currently a noop with a "
            "clear status line."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Operator-supplied reason for the recycle — persisted into "
            "the system_operation_log payload so the Audit tab shows it."
        ),
    )
