# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Background Tasks (scheduler) wire models.

Kept apart from `system_ops.py` so this section stays in one
file and concurrent slices (Connections, Sessions, …) don't have to
rebase on the same module. The parent `system_ops.py` re-exports the
public names so callers and OpenAPI tooling don't see two namespaces.

The `SchedulerActionRequest` body model already lives in
`system_ops.py` and is re-used unchanged here — the action
verb (trigger / pause / resume) rides on the URL path, not the body,
so one request shape covers all three endpoints.

Status semantics:

  * `paused`: literal pause flag from `scheduler_runtime_state`.
    Source of truth for whether the loop will short-circuit its
    next tick.
  * `state`: derived 'running' / 'paused' / 'unknown' triple, ready
    for badge rendering. 'unknown' shows when we have no recent
    audit-trail activity for the scheduler — neither operator
    intervention nor a manual trigger — so we can't say the loop
    is alive without a per-tick beacon row, which is not written.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Derived status triple — kept Python-side as a Literal so the OpenAPI
# schema enumerates them explicitly. 'unknown' is the conservative
# default for schedulers with no observed activity in either the
# runtime-state row or the audit trail.
SchedulerState = Literal["running", "paused", "unknown"]


class SchedulerStatus(BaseModel):
    """One row in the Background Tasks page table.

    `paused_*` and `resumed_*` fields come straight off
    `scheduler_runtime_state`. `last_run_at` is derived from the
    most-recent matching `system_operation_log` row (trigger / pause /
    resume) — it's the latest observable activity from the control
    plane's vantage point. A future beacon row will supplement this
    with the scheduler's own tick timestamp.
    """

    model_config = ConfigDict(extra="forbid")

    scheduler_name: str = Field(
        description=(
            "Code-side scheduler name. Must be one of the "
            "KNOWN_SCHEDULERS constants — the UI dropdown and the "
            "Trigger/Pause/Resume endpoints reject anything else."
        ),
    )
    paused: bool = Field(
        description=(
            "Persisted pause flag. True means the loop short-circuits "
            "its next tick without doing work."
        ),
    )
    paused_at: Optional[str] = Field(
        default=None,
        description="When `paused` last transitioned to True (ISO-8601).",
    )
    paused_by: Optional[str] = Field(
        default=None,
        description="Admin id that pressed Pause.",
    )
    pause_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why the scheduler is paused: the `reason` the pause was given. "
            "Null when the scheduler is not paused, or was paused without a reason."
        ),
    )
    resumed_at: Optional[str] = Field(
        default=None,
        description="When `paused` last transitioned to False (ISO-8601).",
    )
    resumed_by: Optional[str] = Field(
        default=None,
        description="Admin id that pressed Resume.",
    )
    last_run_at: Optional[str] = Field(
        default=None,
        description=(
            "Most recent observable activity from system_operation_log — "
            "manual trigger or pause/resume click. Null for schedulers "
            "with no audit history yet."
        ),
    )
    last_run_result: Optional[str] = Field(
        default=None,
        description=(
            "Reserved for the upcoming beacon row's success/failure tag. "
            "Always null until that row lands."
        ),
    )
    next_run_eta: Optional[str] = Field(
        default=None,
        description=(
            "Reserved for the upcoming beacon row's next-tick estimate. "
            "Always null until the scheduler interval registry ships."
        ),
    )
    state: SchedulerState = Field(
        description=(
            "Badge-ready derivation: 'paused' if paused=True, 'running' "
            "if we have any recent activity, otherwise 'unknown'."
        ),
    )


class SchedulerListResponse(BaseModel):
    """GET /v1/admin/system/tasks body.

    Returned as a `items: [...]` envelope (not a bare list) so a
    future top-level field (e.g. cluster-wide pause toggle, scheduler
    inventory hash for cache validation) can be added without a wire
    break.
    """

    model_config = ConfigDict(extra="forbid")

    items: List[SchedulerStatus]
