# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Health and assurance

## One number, five components, always decomposable

```
health = min(liveness, behaviour, reliability, cost, conformance)
```

## Two windows: the period, and now

Every component is graded over the health period (30 days, fixed — it does
not follow a dashboard's range selector). A rate over 30 days cannot see an
outage that started an hour ago, so the system's latest finished runs are
also judged by THE down rule (`ai_system_down_rule`): all of the latest 3
failed ⇒ `failing`; some of them ⇒ reliability capped at the degraded score.

**Worst-of, not average.** A system that is cheap, fast and *dead* is not
80% healthy — averaging is how a dashboard reports a comfortable number for
a system nobody can call. The minimum is the only aggregation that cannot
be gamed by three good components hiding a fatal one.

And a score nobody can decompose is a score nobody trusts, so every
component is stored alongside the total. The tile shows one number; the
panel shows why.

## `unknown` is a state, not a zero

A system with too little history, or one whose call chain we could only
partly observe, is `unknown` — never rendered green and never scored as
though the absence of bad news were good news. Chain integrity is displayed
next to the score for the same reason: if the platform could only see 60%
of a system's runs intact, the customer has to know that *before* deciding
what the number is worth.

## The Assurance Report

One immutable snapshot per capture, answering the four guarantees with
evidence. The platform engineer opens it at 09:00 and the auditor exports
it in December — the same artifact serves both, which is rare enough to
build the messaging on.

It generalises the asset factsheet from "config + usage" to
"system + runtime behaviour + assurance", and supersedes it for AI Systems.
The factsheet remains the artifact for standalone assets.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

# ---------------------------------------------------------------------------
# Health states
# ---------------------------------------------------------------------------

HEALTH_HEALTHY = "healthy"
HEALTH_LEARNING = "learning"      # baselines still forming; not a clean bill
HEALTH_DEGRADED = "degraded"      # reliability or cost has slipped
HEALTH_DRIFTING = "drifting"      # an open behavioural finding
HEALTH_STALLED = "stalled"        # running, but not finishing
HEALTH_FAILING = "failing"        # running, and every latest run errors
HEALTH_SILENT = "silent"          # stopped calling entirely
HEALTH_SUSPENDED = "suspended"    # an operator or the closed loop turned it off
HEALTH_UNKNOWN = "unknown"        # too little data, or a chain we cannot trust

HEALTH_STATES = frozenset({
    HEALTH_HEALTHY,
    HEALTH_LEARNING,
    HEALTH_DEGRADED,
    HEALTH_DRIFTING,
    HEALTH_STALLED,
    HEALTH_FAILING,
    HEALTH_SILENT,
    HEALTH_SUSPENDED,
    HEALTH_UNKNOWN,
})

#: States that must never render as green, whatever the numeric score says.
#: `learning` and `unknown` are in here precisely because they are the two an
#: optimistic UI would round up.
NON_GREEN_STATES = frozenset({
    HEALTH_LEARNING,
    HEALTH_DEGRADED,
    HEALTH_DRIFTING,
    HEALTH_STALLED,
    HEALTH_FAILING,
    HEALTH_SILENT,
    HEALTH_SUSPENDED,
    HEALTH_UNKNOWN,
})

#: Below this share of intact chains, the score is not trustworthy and the
#: state is forced to `unknown`. Two-thirds is a judgement call, and the
#: point is that *some* floor exists: a score computed from a third of the
#: evidence is a guess wearing a number's clothes.
MIN_CHAIN_INTEGRITY_FOR_SCORE = 0.67

#: Components, in the order the UI shows them.
COMPONENTS = (
    "liveness",
    "behaviour",
    "reliability",
    "cost",
    "conformance",
    # The human control loop around the machine. `None` whenever no
    # approval gate was exercised in the window, which degrades nothing:
    # an un-gated system has no oversight *requirement* to fail.
    "oversight",
    # Human review of assurance-inbox findings. `None` until the
    # system has ever had an inbox item (unmeasured is not a free pass and
    # not a failure); 0.5 while an open critical item is past the review
    # SLA; 1.0 otherwise.
    "review",
)


class AISystemHealth(Base):
    """Materialised current state — one row per system, cheap for tiles."""

    __tablename__ = "ai_system_health"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(String(255), nullable=False, index=True)

    health_state = Column(
        String(16), nullable=False, default=HEALTH_UNKNOWN,
        server_default=HEALTH_UNKNOWN,
    )
    health_score = Column(
        Float,
        nullable=True,
        comment=(
            "min() of the components, not their mean. A system that is "
            "cheap, fast and dead is not 80% healthy. NULL when the state "
            "is unknown — a number would imply evidence we do not have."
        ),
    )

    liveness_score = Column(Float, nullable=True)
    behaviour_score = Column(Float, nullable=True)
    reliability_score = Column(Float, nullable=True)
    cost_score = Column(Float, nullable=True)
    conformance_score = Column(Float, nullable=True)
    #: NULL means "no approval gate exercised in the window" — not a
    #: failing score. See services/ai_system_oversight.py.
    oversight_score = Column(Float, nullable=True)
    #: NULL means "never had an inbox item"; 0.5 = review debt
    #: (open critical past SLA). See services/assurance_inbox.py.
    review_score = Column(Float, nullable=True)

    chain_integrity_pct = Column(
        Float,
        nullable=True,
        comment=(
            "Share of runs observed intact. Shown beside the score: if we "
            "could only see 60% of a system's runs, the customer must know "
            "that before trusting the number."
        ),
    )
    baseline_state = Column(String(16), nullable=True)

    # The card judgments, materialized on the health tick so the
    # Mission Control landing page reads one row per system instead of
    # computing verdicts per render. Judgments are PINNED to the standard
    # 30-day window regardless of the dashboard's window selector.
    verdict_status = Column(
        String(32),
        nullable=True,
        comment="Period verdict (assured | assured_with_exceptions | "
        "violations | insufficient_evidence), 30-day standard window.",
    )
    t_class = Column(
        String(12),
        nullable=True,
        comment="Big-T workload class measured from the run ledger "
        "(T(1) | T(n) | T(n·k) | T(n·k·a)); NULL below evidence minimum.",
    )
    k_median = Column(Float, nullable=True, comment="Median model calls per task")
    a_max = Column(
        Integer, nullable=True, comment="Max agent delegation depth in window"
    )

    runs_24h = Column(Integer, nullable=False, default=0, server_default="0")
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    open_drift_events = Column(Integer, nullable=False, default=0, server_default="0")

    #: Why the state is what it is, in the operator's words. A state with no
    #: explanation sends someone hunting through four other panels.
    reasons = Column(JSONB, nullable=True)

    #: The critical-dependency cap applied to
    #: ``health_state``, stored by the SAME computation that stored the state
    #: (``{state, reason, dependencies: [...]}``; NULL = no cap). The health
    #: endpoint reads it; recomputing it live could disagree with the stored
    #: state for a whole sweep interval.
    dependency_cap = Column(
        JSONB,
        nullable=True,
        comment="The critical-dependency cap applied to health_state by the "
        "same computation; NULL = no cap",
    )

    #: The down rule over the system's own latest finished runs, stored by
    #: the SAME computation that stored the state (like `dependency_cap`):
    #: ``{verdict, considered, failed, latest: [...], window_success_rate,
    #: window_hours}``; NULL = not judged (a shared service, scored from the
    #: calls it served). See `ai_system_assurance._recent_runs`.
    recent_runs = Column(
        JSONB,
        nullable=True,
        comment="Down-rule verdict over the latest finished runs, stored with "
        "health_state by the same computation; NULL = not judged",
    )

    computed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("tenant_id", "ai_system_id", name="uq_ai_system_health_key"),
        Index("ix_ai_system_health_state", "tenant_id", "health_state"),
        {
            "comment": (
                "Materialised health for one AI System. "
                "Worst-of across five components, always decomposable."
            )
        },
    )

    @validates("health_state")
    def _v_state(self, _k: str, v: str) -> str:
        if v not in HEALTH_STATES:
            raise ValueError(
                f"health_state must be one of {sorted(HEALTH_STATES)}, got {v!r}"
            )
        return v

    @property
    def is_green(self) -> bool:
        return self.health_state == HEALTH_HEALTHY

    @property
    def is_trustworthy(self) -> bool:
        """Whether the score rests on enough intact evidence to mean anything."""
        if self.chain_integrity_pct is None:
            return False
        return self.chain_integrity_pct >= MIN_CHAIN_INTEGRITY_FOR_SCORE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "health_state": self.health_state,
            "health_score": self.health_score,
            "liveness_score": self.liveness_score,
            "behaviour_score": self.behaviour_score,
            "reliability_score": self.reliability_score,
            "cost_score": self.cost_score,
            "conformance_score": self.conformance_score,
            "oversight_score": self.oversight_score,
            "review_score": self.review_score,
            "chain_integrity_pct": self.chain_integrity_pct,
            "baseline_state": self.baseline_state,
            "verdict_status": self.verdict_status,
            "t_class": self.t_class,
            "k_median": self.k_median,
            "a_max": self.a_max,
            "runs_24h": self.runs_24h,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "open_drift_events": self.open_drift_events,
            "dependency_cap": self.dependency_cap,
            "recent_runs": self.recent_runs,
            "reasons": self.reasons or [],
            "is_green": self.is_green,
            "is_trustworthy": self.is_trustworthy,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


# ---------------------------------------------------------------------------
# Assurance reports
# ---------------------------------------------------------------------------

REPORT_TRIGGER_MANUAL = "manual"
REPORT_TRIGGER_SCHEDULED = "scheduled"
REPORT_TRIGGER_EXPORT = "export"
REPORT_TRIGGER_LIFECYCLE = "lifecycle"  # captured at a stage transition

REPORT_TRIGGERS = frozenset({
    REPORT_TRIGGER_MANUAL,
    REPORT_TRIGGER_SCHEDULED,
    REPORT_TRIGGER_EXPORT,
    REPORT_TRIGGER_LIFECYCLE,
})


class AISystemAssuranceReport(Base):
    """An immutable snapshot answering the four guarantees with evidence."""

    __tablename__ = "ai_system_assurance_reports"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(String(255), nullable=False, index=True)

    period_hours = Column(
        Integer,
        nullable=False,
        comment="Evidence window (0 = all time). Stored so a snapshot is self-describing.",
    )

    report_json = Column(
        JSONB,
        nullable=False,
        comment=(
            "The assembled report. Immutable: it is evidence, and evidence "
            "that changes when the system does is not evidence."
        ),
    )

    #: Denormalised for listing without parsing the blob.
    health_state = Column(String(16), nullable=True)
    contract_version = Column(Integer, nullable=True)
    contract_hash = Column(String(80), nullable=True)

    trigger = Column(
        String(24), nullable=False, default=REPORT_TRIGGER_MANUAL,
        server_default=REPORT_TRIGGER_MANUAL,
    )
    captured_by = Column(String(255), nullable=True)

    signature_json = Column(
        JSONB,
        nullable=True,
        comment=(
            "Ed25519 attestation over the captured report "
            "(sha256 manifest signed by the deployment audit key, same key "
            "as audit-chain checkpoints). Best-effort at capture time: "
            "{signed: bool, sha256, signature?, public_key?, "
            "unsigned_reason?}. An unsigned snapshot still carries its "
            "digest; `signed: false` records which of the two a recipient "
            "is holding — it is never implied."
        ),
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        Index(
            "ix_assurance_reports_system_created",
            "tenant_id", "ai_system_id", "created_at",
        ),
        {
            "comment": (
                "Immutable assurance snapshot. The "
                "artifact an engineer reads at 09:00 and an auditor exports "
                "in December."
            )
        },
    )

    @validates("trigger")
    def _v_trigger(self, _k: str, v: str) -> str:
        if v not in REPORT_TRIGGERS:
            raise ValueError(
                f"trigger must be one of {sorted(REPORT_TRIGGERS)}, got {v!r}"
            )
        return v

    def to_dict(self, include_report: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "period_hours": self.period_hours,
            "health_state": self.health_state,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "trigger": self.trigger,
            "captured_by": self.captured_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # Attestation status without the blob: a listing must be
            # able to say which snapshots are certificates and which are
            # merely records.
            "signed": bool((self.signature_json or {}).get("signed")),
            "verdict_status": (
                ((self.report_json or {}).get("verdict") or {}).get("status")
            ),
        }
        if include_report:
            data["report_json"] = self.report_json or {}
        return data


def score_health(
    *,
    liveness: Optional[float] = None,
    behaviour: Optional[float] = None,
    reliability: Optional[float] = None,
    cost: Optional[float] = None,
    conformance: Optional[float] = None,
    oversight: Optional[float] = None,
    review: Optional[float] = None,
    chain_integrity_pct: Optional[float] = None,
    is_suspended: bool = False,
    is_silent: bool = False,
    is_stalled: bool = False,
    is_learning: bool = False,
    open_drift_events: int = 0,
    dependency_cap: Optional[Dict[str, Any]] = None,
    dependency_reasons: Optional[List[str]] = None,
    recent_runs_verdict: Optional[str] = None,
    recent_runs_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """The whole scoring judgement, as a pure function.

    Ordered by what an operator needs to act on first, not by numeric
    severity. A suspended system is `suspended` even if every component
    reads perfectly — its scores describe traffic that is no longer being
    allowed, and calling that "healthy" would be absurd.
    """
    components = {
        "liveness": liveness,
        "behaviour": behaviour,
        "reliability": reliability,
        "cost": cost,
        "conformance": conformance,
        "oversight": oversight,
        # Review debt; None when unmeasured, degrades under
        # worst-of exactly like any other component.
        "review": review,
    }
    known = [v for v in components.values() if v is not None]
    # Worst-of. A system that is cheap, fast and dead is not 80% healthy.
    score = min(known) if known else None
    reasons: List[str] = []

    # --- states that override the number ---------------------------------
    if is_suspended:
        return _result(HEALTH_SUSPENDED, None, components, chain_integrity_pct,
                       ["Every call from this system is currently denied."])

    if chain_integrity_pct is not None and chain_integrity_pct < MIN_CHAIN_INTEGRITY_FOR_SCORE:
        return _result(
            HEALTH_UNKNOWN, None, components, chain_integrity_pct,
            [
                f"Only {chain_integrity_pct:.0%} of this system's runs were observed "
                f"intact, which is not enough to score it honestly."
            ],
        )

    if not known:
        return _result(
            HEALTH_UNKNOWN, None, components, chain_integrity_pct,
            ["No signals have enough data yet."],
        )

    if is_silent:
        reasons.append("This system has stopped calling through the gateway.")
        return _result(HEALTH_SILENT, score, components, chain_integrity_pct, reasons)

    if is_stalled:
        reasons.append("Runs are starting but not finishing.")
        return _result(HEALTH_STALLED, score, components, chain_integrity_pct, reasons)

    if recent_runs_verdict == "down":
        # The system is calling and every one of its latest finished runs
        # failed: it is down NOW, whatever its window rate says. After
        # silent and stalled (no runs / no endings is the deeper fault),
        # before drift and a dependency cap (both are "worse than usual";
        # this is "not working"). The caller has already capped reliability
        # at 0, so the number agrees with the word.
        reasons.append(
            recent_runs_reason or "Every one of this system's latest runs failed."
        )
        return _result(HEALTH_FAILING, score, components, chain_integrity_pct, reasons)

    if recent_runs_verdict == "degraded" and recent_runs_reason:
        # Explained here; the capped reliability component degrades the
        # state below (or drift / a dependency cap names a worse one).
        reasons.append(recent_runs_reason)

    if open_drift_events > 0:
        reasons.append(
            f"{open_drift_events} open behavioural finding"
            f"{'' if open_drift_events == 1 else 's'}."
        )
        return _result(HEALTH_DRIFTING, score, components, chain_integrity_pct, reasons)

    if dependency_cap is not None:
        # A critical dependency that is not healthy caps the
        # verdict at degraded: a system is no healthier than a dependency it
        # cannot work without (SRE). After the system's OWN fault states
        # (silent, stalled, drifting are worse and stay), before learning
        # and healthy, which it overrides.
        reasons.extend(
            dependency_reasons
            or [f"Critical dependency {dependency_cap.get('name')} is not healthy."]
        )
        out = _result(HEALTH_DEGRADED, score, components, chain_integrity_pct, reasons)
        out["capped_by"] = dict(dependency_cap)
        return out

    learning_reason = (
        "Still learning this system's normal — not yet a clean bill of health."
    )
    if score is not None and score < 0.8:
        # Before learning: learning only means the BEHAVIOUR baselines are
        # not ready (behaviour is unscored until they are). Reliability,
        # liveness, conformance and cost are measured without a baseline, so
        # a score this low is a measured fault, not a lack of history — a
        # system failing most of its runs must not read "learning".
        worst = min(
            ((k, v) for k, v in components.items() if v is not None),
            key=lambda kv: kv[1],
        )
        reasons.append(f"{worst[0]} is the weakest component at {worst[1]:.0%}.")
        if is_learning:
            reasons.append(learning_reason)
        return _result(HEALTH_DEGRADED, score, components, chain_integrity_pct, reasons)

    if is_learning:
        # Deliberately *after* the fault states: a system can be both still
        # learning and already silent, and silence is the more urgent fact.
        reasons.append(learning_reason)
        return _result(HEALTH_LEARNING, score, components, chain_integrity_pct, reasons)

    return _result(HEALTH_HEALTHY, score, components, chain_integrity_pct, reasons)


def _result(state, score, components, chain, reasons) -> Dict[str, Any]:
    return {
        "health_state": state,
        "health_score": score,
        "chain_integrity_pct": chain,
        "reasons": reasons,
        # Set only when a dependency capped the state.
        "capped_by": None,
        **{f"{k}_score": v for k, v in components.items()},
    }


__all__ = [
    "COMPONENTS",
    "HEALTH_DEGRADED",
    "HEALTH_DRIFTING",
    "HEALTH_FAILING",
    "HEALTH_HEALTHY",
    "HEALTH_LEARNING",
    "HEALTH_SILENT",
    "HEALTH_STALLED",
    "HEALTH_STATES",
    "HEALTH_SUSPENDED",
    "HEALTH_UNKNOWN",
    "MIN_CHAIN_INTEGRITY_FOR_SCORE",
    "NON_GREEN_STATES",
    "REPORT_TRIGGERS",
    "REPORT_TRIGGER_MANUAL",
    "REPORT_TRIGGER_SCHEDULED",
    "AISystemAssuranceReport",
    "AISystemHealth",
    "score_health",
]
