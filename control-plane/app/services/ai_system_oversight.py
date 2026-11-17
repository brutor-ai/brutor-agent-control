# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Oversight — scoring whether human control was *effective*, not just present

The five original signals score the machine. This one scores the human
control loop around it, because "a human approved it" and "a human
exercised judgement" are different claims and only the second one is
oversight.

Brutor already recorded every approval request. It counted them into the
report's exception totals and stopped there — so the platform could
evidence that approvals *happened* and could not evidence that oversight
*worked*. That is the gap this closes, and it is the difference between
gesturing at EU AI Act Article 14 and evidencing it.

## What is scored, and what is deliberately not

**Scored — lapse rate.** An approval that expires is a human who was
asked and never answered. It is the one oversight failure with no benign
reading, so it is the spine of the score.

**Scored — approver concentration.** When every decision in the period
routes through one person, the control is a single point of failure
whether or not that person is diligent. Capped, not zeroed: it is a risk,
not a fault.

**Disclosed but NOT scored — decision latency and denial rate.** The
tempting metric here is "rubber-stamping": approvals resolved suspiciously
fast, or a denial rate of zero. Both are real risks and neither has an
honest absolute threshold — a fast approval may be an alert, well-staffed
team, and a genuinely well-behaved system legitimately gets approved every
time. Inventing a bound would manufacture findings, which is the failure
mode this codebase exists to avoid.

Instead these are published as baseline metric keys
(`approval_latency_p50`, `approval_denial_rate`) so the *existing* drift
machinery learns each system's own normal and flags **movement** —
"this team used to deliberate for four minutes and now answers in six
seconds" — with a cause attached. Change is detectable; an absolute
threshold is not defensible. Rubber-stamping is therefore caught as drift,
not as a hardcoded rule.

## Incident windows excuse the rate, never the record

Oversight is a RATE — holds decided over holds raised — so it is graded
like the error rate: a hold RAISED inside a declared incident window of
its AI System (or a tenant-wide one) is not judged. The count of such
holds is always stated beside the rate ("N inside incident windows, not
judged"), and the raw counts (`requested`, `expired`, …) stay complete:
excluded is not hidden. A window only excuses what it covers — a hold
raised outside every window that lapses because nobody decided it in
time (overnight, say) is a configuration or operations matter and always
counts. There is no other excuse for a lapsed hold. The one predicate is
`ai_system_judgement_exclusion.judged_holds`.

## Not-applicable is not a failure

A system that requested no approvals in the window scores `None`, never
zero. No approval gate configured means no oversight *requirement* was
observed — that is a contract question ("should this system have gates?"),
not an oversight-effectiveness question, and conflating them would punish
every fully-autonomous system for a design decision its owner made
deliberately. `None` degrades nothing in the worst-of roll-up, matching
every other signal's insufficient-evidence rule.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.constants import approvals as approval_status
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_holds

# ---------------------------------------------------------------------------
# Vocabulary + thresholds
# ---------------------------------------------------------------------------

# Status vocabulary: `app/constants/approvals.py` (`approval_status`). A hold
# is classified by `approval_status.decision()`, never by comparing the stored
# status here: `consumed` is an approval, and a `pending` row past its deadline
# has lapsed whether or not the sweeper has rewritten it.

#: Below this many resolved decisions the concentration check is not run.
#: One approver out of two decisions is a rota, not a single point of
#: failure; the same honesty rule the classifier uses for small samples.
MIN_DECISIONS_FOR_CONCENTRATION = 5

#: Ceiling applied when every decision in the period ran through one
#: person. Deliberately a cap and not a floor of 0: concentrated oversight
#: is a standing risk to surface, not an outage.
CONCENTRATION_CEILING = 0.8

#: Baseline metric keys published for the drift machinery to learn. These
#: are the rubber-stamping detectors — by movement, never by threshold.
METRIC_APPROVAL_LATENCY_P50 = "approval_latency_p50"
METRIC_APPROVAL_DENIAL_RATE = "approval_denial_rate"

OVERSIGHT_METRIC_KEYS = (
    METRIC_APPROVAL_LATENCY_P50,
    METRIC_APPROVAL_DENIAL_RATE,
)


def _latency_seconds(row: ToolApprovalRequest) -> Optional[float]:
    """Seconds between request and human decision, when both are known."""
    created: Optional[datetime] = row.created_at
    resolved: Optional[datetime] = row.resolved_at
    if created is None or resolved is None:
        return None
    delta = (resolved - created).total_seconds()
    # A negative delta means clock skew between writers, not a decision
    # made before it was requested. Drop rather than propagate nonsense.
    return delta if delta >= 0 else None


def gather_oversight(
    db: Session,
    tenant_id: str,
    group_ids: List[str],
    since: datetime,
) -> Dict[str, Any]:
    """Collect the raw oversight facts for one system's groups.

    `group_ids` is the AI System's group plus (optionally) descendants —
    approvals are recorded against the *requesting* group, so a system
    whose work happens in child groups still sees its own approvals.
    """
    if not group_ids:
        return _empty_facts()

    rows: List[ToolApprovalRequest] = (
        db.query(ToolApprovalRequest)
        .filter(
            ToolApprovalRequest.tenant_id == tenant_id,
            ToolApprovalRequest.requester_group_id.in_(group_ids),
            ToolApprovalRequest.created_at >= since,
        )
        .all()
    )
    if not rows:
        return _empty_facts()

    # The rate is graded over holds raised OUTSIDE every
    # declared incident window; the record (the counts below) is complete.
    flags = judged_holds(
        db, tenant_id, [(r.requester_group_id, r.created_at) for r in rows],
        purpose=PURPOSE_RATE,
    )
    judged = [r for r, keep in zip(rows, flags) if keep]
    now = datetime.now(timezone.utc)
    counts = _counts(rows, now)
    graded = _counts(judged, now)

    latencies = [
        s
        for s in (
            _latency_seconds(r) for r in judged if approval_status.is_decided(r.status)
        )
        if s is not None
    ]
    approvers = {
        r.resolved_by
        for r in judged
        if approval_status.is_decided(r.status) and r.resolved_by
    }

    return {
        # Every hold raised in the period — the record, never filtered.
        **counts,
        # Raised inside a declared incident window: recorded, not judged.
        "excluded_in_incident_windows": len(rows) - len(judged),
        # The holds the rates below are graded on.
        "judged": graded,
        # Lapse = asked and never answered. `pending` is excluded: a request
        # still inside its window has not lapsed, it is simply in flight.
        "lapse_rate": (
            (graded["expired"] / graded["requested"]) if graded["requested"] else None
        ),
        "denial_rate": (
            (graded["rejected"] / graded["decided"]) if graded["decided"] else None
        ),
        "latency_p50_seconds": (
            statistics.median(latencies) if latencies else None
        ),
        "distinct_approvers": len(approvers),
    }


def _counts(rows: List[ToolApprovalRequest], now: Optional[datetime] = None) -> Dict[str, int]:
    decisions = [approval_status.decision(r.status, r.expires_at, now) for r in rows]
    approved = decisions.count(approval_status.APPROVED)
    rejected = decisions.count(approval_status.REJECTED)
    return {
        "requested": len(rows),
        "approved": approved,
        "rejected": rejected,
        "expired": decisions.count(approval_status.EXPIRED),
        "pending": decisions.count(approval_status.PENDING),
        "decided": approved + rejected,
    }


def _empty_facts() -> Dict[str, Any]:
    return {
        **_counts([]),
        "excluded_in_incident_windows": 0,
        "judged": _counts([]),
        "lapse_rate": None,
        "denial_rate": None,
        "latency_p50_seconds": None,
        "distinct_approvers": 0,
    }


def score_oversight(facts: Dict[str, Any]) -> Optional[float]:
    """The oversight component, or `None` when it does not apply.

    `None` means "no oversight requirement was observed in this window" —
    it degrades nothing, exactly like every other signal's insufficient-
    evidence path. Only a system that actually asked for human decisions
    can be scored on how well those decisions were made.

    Graded on the JUDGED holds (`facts["judged"]`): holds
    raised inside a declared incident window are not judged, so a period
    whose every hold lies inside one is unmeasured (`None`), not passed.
    """
    graded = facts["judged"]
    requested = graded.get("requested") or 0
    if requested == 0:
        return None

    lapse_rate = facts.get("lapse_rate")
    score = 1.0 if lapse_rate is None else max(0.0, 1.0 - lapse_rate)

    # Concentration is a ceiling, applied only with enough decisions for
    # "one approver" to mean something.
    decided = graded.get("decided") or 0
    approvers = facts.get("distinct_approvers") or 0
    if decided >= MIN_DECISIONS_FOR_CONCENTRATION and approvers == 1:
        score = min(score, CONCENTRATION_CEILING)

    return score


def excluded_text(excluded: int) -> str:
    """The disclosure beside every oversight rate: what was not judged."""
    return (
        f"{excluded} approval request(s) raised inside declared incident "
        f"windows: recorded, not judged."
    )


def oversight_reasons(facts: Dict[str, Any]) -> List[str]:
    """Operator-facing explanation of what dragged the score, if anything —
    and, whenever holds were excluded, how many (excluded is not hidden)."""
    reasons: List[str] = []
    graded = facts["judged"]
    excluded = facts.get("excluded_in_incident_windows") or 0
    requested = graded.get("requested") or 0

    expired = graded.get("expired") or 0
    if expired:
        judged_word = "judged " if excluded else ""
        reasons.append(
            f"{expired} of {requested} {judged_word}approval request(s) expired "
            f"without a human decision."
        )
    if excluded:
        reasons.append(excluded_text(excluded))

    decided = graded.get("decided") or 0
    approvers = facts.get("distinct_approvers") or 0
    if requested and decided >= MIN_DECISIONS_FOR_CONCENTRATION and approvers == 1:
        reasons.append(
            f"All {decided} decisions in the period were made by a single "
            f"approver — the control has no redundancy."
        )
    return reasons


def oversight_report_section(facts: Dict[str, Any]) -> Dict[str, Any]:
    """The disclosed block for the assurance report.

    Publishes the un-scored metrics beside the scored ones so a reader can
    see *why* the number is what it is — and can judge latency and denial
    rate themselves, which the platform deliberately declines to threshold.
    The counts are the complete record; the rates are over `judged` holds,
    with the holds inside incident windows stated beside them.
    """
    graded = facts["judged"]
    requested = facts.get("requested") or 0
    excluded = facts.get("excluded_in_incident_windows") or 0
    applicable = (graded.get("requested") or 0) > 0
    if applicable:
        means = "Human oversight of this system's gated actions."
    elif requested:
        means = (
            "Every approval request in this period was raised inside a declared "
            "incident window: recorded, not judged. Oversight is unmeasured for "
            "the period, not passed."
        )
    else:
        means = (
            "No approval gates were exercised in this period, so "
            "oversight effectiveness cannot be measured. This is a "
            "contract question, not an oversight failure."
        )
    return {
        "applicable": applicable,
        "means": means,
        # The record — every hold raised in the period.
        "requested": requested,
        "approved": facts.get("approved", 0),
        "rejected": facts.get("rejected", 0),
        "expired": facts.get("expired", 0),
        "pending": facts.get("pending", 0),
        # Raised inside a declared incident window: recorded,
        # not judged. The rates below are over `judged` only.
        "excluded_in_incident_windows": excluded,
        "excluded_statement": excluded_text(excluded) if excluded else None,
        "judged": dict(graded),
        "lapse_rate": facts.get("lapse_rate"),
        # Disclosed, not scored — see the module docstring.
        "denial_rate": facts.get("denial_rate"),
        "latency_p50_seconds": facts.get("latency_p50_seconds"),
        "distinct_approvers": facts.get("distinct_approvers", 0),
        "not_scored": [
            "denial_rate",
            "latency_p50_seconds",
        ],
        "not_scored_note": (
            "Decision latency and denial rate are recorded and trended but "
            "never thresholded: a fast approval may be a responsive team, "
            "and a well-behaved system is legitimately approved every time. "
            "Movement in either is detected as drift against this system's "
            "own history instead."
        ),
    }


__all__ = [
    "CONCENTRATION_CEILING",
    "METRIC_APPROVAL_DENIAL_RATE",
    "METRIC_APPROVAL_LATENCY_P50",
    "MIN_DECISIONS_FOR_CONCENTRATION",
    "OVERSIGHT_METRIC_KEYS",
    "excluded_text",
    "gather_oversight",
    "oversight_reasons",
    "oversight_report_section",
    "score_oversight",
]
