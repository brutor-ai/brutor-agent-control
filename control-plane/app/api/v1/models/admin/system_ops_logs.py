# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the System Operations Console Logs sub-section.

Kept in a sibling file (not in `system_ops.py`) so each phase's
additions sit in their own module — diff readability when reviewing
a single phase outweighs the "one big file is searchable" win.

Wire surface here is small: one entry shape returned by the GET, one
request shape consumed by the POST. The WebSocket tail is JSON-blob
free-form on the wire and intentionally untyped at the Pydantic
layer — the line shape is documented in `system_logs_ws.py` and the
admin UI types mirror it there. Validating each tail line through
Pydantic would be a perf trap for a hot streaming path.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Canonical wire-format level enum
# ---------------------------------------------------------------------------
# Five rungs the slider exposes. We accept TRACE for Rust parity but
# coerce to DEBUG on the Python side (see services/system_ops/logs.py).
# `warning` / `critical` / `fatal` are accepted as aliases at runtime;
# this enum is what the wire surface enforces.

LogLevel = Literal["trace", "debug", "info", "warn", "error"]


class LogLevelEntry(BaseModel):
    """One row in the GET /v1/admin/system/logs/levels response.

    Surfaces both the LIVE effective level (what the next log call
    will dispatch against) and the persisted OVERRIDE — they should
    agree, but if they drift the UI shows a yellow dot so an operator
    can spot a manual DB edit / boot-replay glitch immediately.
    """

    model_config = ConfigDict(extra="forbid")

    module: str = Field(
        description="Python logger name. Matches the BrutorLogger constructor arg.",
    )
    level: LogLevel = Field(
        description=(
            "Live effective level as reported by "
            "`logging.getLogger(module).getEffectiveLevel()`."
        ),
    )
    effective_level_int: int = Field(
        ge=0,
        description="Numeric level for clients that want exact equality checks.",
    )
    is_curated: bool = Field(
        description=(
            "True when the module appears in the curated slider list — "
            "the UI renders these prominently, ad-hoc overrides below."
        ),
    )
    override_level: Optional[LogLevel] = Field(
        default=None,
        description="Persisted override level. NULL when no override row exists.",
    )
    override_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp the override was last written.",
    )
    override_by: Optional[str] = Field(
        default=None,
        description=(
            "Admin id that applied the override. Currently always NULL — "
            "attribution lives in the audit log, this surface is a "
            "snapshot for the Logs page only."
        ),
    )


class LogLevelListResponse(BaseModel):
    """GET /v1/admin/system/logs/levels wrapper."""

    model_config = ConfigDict(extra="forbid")

    items: list[LogLevelEntry]


class SetLogLevelRequest(BaseModel):
    """Body for POST /v1/admin/system/logs/levels."""

    model_config = ConfigDict(extra="forbid")

    module: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Python logger name to mutate. Open enum — any name "
            "accepted; UI uses the curated list to fill the slider grid."
        ),
    )
    level: LogLevel = Field(
        description=(
            "Target level. NOTE: trace collapses to debug on Python; "
            "the Rust runtime honours trace directly via tracing-subscriber."
        ),
    )
    scope: Literal["all", "self"] = Field(
        default="all",
        description=(
            "'all' — fan out via NOTIFY so every replica picks up the change. "
            "'self' — mutate only the replica that received the request. "
            "Use 'self' when troubleshooting a single replica under load."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Operator-supplied reason. Persisted into the system_operation_log "
            "row so the Audit tab shows it."
        ),
    )


class SetLogLevelResponse(BaseModel):
    """Echo + effective-state response for the POST endpoint."""

    model_config = ConfigDict(extra="forbid")

    module: str
    level: LogLevel
    effective_level_int: int
    applied_at: str = Field(description="ISO-8601 timestamp the mutation landed.")
    fanout_channel: str = Field(
        description="NOTIFY channel the change was published on (for client UI breadcrumbs).",
    )
