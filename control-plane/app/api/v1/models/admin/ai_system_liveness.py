# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Liveness API models

Mirrors `ai_system_liveness_expectations` plus the learned-cadence
proposal.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

LivenessMode = Literal["continuous", "scheduled", "sporadic", "on_demand"]
#: Mirrors app.services.ai_system_liveness_state.LIVENESS_STATES.
LivenessStateName = Literal["alive", "silent", "idle", "unstarted", "not_configured"]


class LivenessExpectationResponse(BaseModel):
    """The stored (or defaulted) expectation for one AI System."""

    id: str = Field("", description="Empty when nothing has been saved yet")
    tenant_id: Optional[str] = None
    ai_system_id: str

    mode: LivenessMode
    expected_min_runs: int
    window_seconds: int
    grace_seconds: int
    enabled: bool
    max_quiet_processed_seconds: Optional[int] = Field(
        None,
        description=(
            "At most this long without a PROCESSED run (an outcome that "
            "counts toward the baseline) before the work_quiet signal; null = not checked"
        ),
    )

    learned_from_runs: Optional[int] = None
    learned_median_gap_seconds: Optional[int] = None
    learned_p95_gap_seconds: Optional[int] = None
    learned_at: Optional[str] = None
    accepted_from_learned: bool = False

    last_seen_run_at: Optional[str] = None
    last_checked_at: Optional[str] = Field(
        None,
        description=(
            "When the detector last evaluated this system. Null means it "
            "has never been checked — which looks identical to 'healthy' "
            "if you only ever record findings."
        ),
    )
    last_alert_at: Optional[str] = None

    alerts_on_silence: bool = False
    deadline_seconds: int = 0
    has_learned_cadence: bool = False

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LivenessExpectationUpdate(BaseModel):
    """PATCH-style update — omitted fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")

    mode: Optional[LivenessMode] = None
    expected_min_runs: Optional[int] = Field(None, ge=1, le=1_000_000)
    window_seconds: Optional[int] = Field(None, ge=60, le=2_592_000)
    grace_seconds: Optional[int] = Field(None, ge=0, le=604_800)
    enabled: Optional[bool] = None
    #: Send null to stop checking.
    max_quiet_processed_seconds: Optional[int] = Field(None, ge=60, le=31_622_400)


class LivenessStatusResponse(BaseModel):
    """Expectation plus what is actually happening right now."""

    ai_system_id: str
    configured: bool = Field(
        ...,
        description=(
            "False when no expectation has been saved; the embedded "
            "expectation then carries the kind-derived default rather than "
            "the endpoint 404-ing."
        ),
    )
    expectation: LivenessExpectationResponse

    # The ONE liveness verdict
    # (app/services/ai_system_liveness_state.py), the same value the health
    # scorer and the AI Systems list read.
    state: LivenessStateName = Field(
        ...,
        description="alive | silent | idle | unstarted | not_configured",
    )
    reason: str = Field(..., description="Why the state is what it is, in one sentence")
    window_hours: int = Field(..., description="Judgment window the state was computed over")
    inbound_calls: int = Field(
        0, description="Inbound calls (run segments) in the window — how a shared service is called"
    )
    last_activity_at: Optional[str] = Field(
        None, description="Latest own run or run segment, whichever is later"
    )
    last_run_at: Optional[str] = None
    quiet_for_seconds: Optional[int] = Field(
        None, description="Seconds since last_activity_at"
    )
    has_ever_run: bool = Field(
        False, description="Any own run or run segment has ever been recorded"
    )
    is_silent: bool = Field(
        False,
        description=(
            "state == 'silent'. Never true for idle, unstarted or "
            "not_configured systems."
        ),
    )
    # Work, not just activity. Its own signal: never part of
    # `state`, never silent.
    last_processed_run_at: Optional[str] = Field(
        None, description="Latest success run — an outcome that counts toward the baseline"
    )
    processed_quiet_for_seconds: Optional[int] = None
    work_quiet: bool = Field(
        False, description="No processed run within max_quiet_processed_seconds (the system still runs)"
    )
    work_quiet_reason: Optional[str] = None


class CadenceProposalResponse(BaseModel):
    """What the platform thinks the expectation should be."""

    mode: LivenessMode
    window_seconds: int
    grace_seconds: int
    expected_min_runs: int
    learned_from_runs: int
    median_gap_seconds: Optional[int] = None
    p95_gap_seconds: Optional[int] = Field(
        None,
        description=(
            "The window is sized from this, not the median: a window at the "
            "median alerts on half of all normal quiet periods."
        ),
    )
    confident: bool = Field(
        ...,
        description=(
            "False when there is too little history for a cadence to mean "
            "anything. /accept refuses in that case rather than saving a "
            "confident-looking guess."
        ),
    )
    rationale: str
