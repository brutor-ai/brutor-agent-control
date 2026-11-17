# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The Assurance Report

One artifact per AI System, answering the four guarantees with evidence.
The platform engineer opens it at 09:00; the auditor exports it in
December. **The same artifact serves both**, which is rare enough that the
messaging is built on it.

## The section that makes it credible is the last one

`not_covered` states, in the report itself, what the platform did **not**
observe: chain integrity below 100%, guardrails that were not re-evaluated
during replay, clients that never traversed the gateway, behaviour we
detect but truth we do not.

That section is not a disclaimer bolted on for legal comfort — it is the
reason the rest of the report is worth reading. A report that claims total
coverage is one an auditor stops trusting the moment they find a gap, and
they always find a gap. Every claim here is bounded by what the gateway
actually observed.

## Immutable by construction

A captured report is frozen JSON. Recomputing it later would answer a
different question — "what is true now" rather than "what was true then" —
and evidence that changes when the system does is not evidence.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_contract import (
    AISystemContract,
    canonical_json,
)
from app.services.ai_system_coverage import attest_coverage, coverage_gaps
from app.services.ai_system_signal_profile import apply_profile, profile_gaps
from app.services.proxy_signing import sign_manifest
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_evidence import (
    EVIDENCE_IMPACT_ASSESSMENT,
    AISystemEvidence,
)
from app.database.models.ai_system_run import (
    CHAIN_CLIENT_ASSERTED,
    CHAIN_OBSERVED_EXTERNAL,
    CHAIN_INTACT,
)
from app.database.models.ai_system_health import (
    AISystemAssuranceReport,
    AISystemHealth,
    REPORT_TRIGGER_MANUAL,
    score_health,
)
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.services.ai_system_liveness_state import liveness_state
from app.database.models.ai_system_replay import AISystemReplayRun, AISystemReplaySuite
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import ResourceGroup
from app import standards
from app.services import ai_system_conformance as conformance_service
from app.services import ai_system_contract_service as contracts
from app.services import ai_system_envelope as envelope_service
from app.services import ai_system_oversight as oversight_service
from app.services import ai_system_composite_compliance as composite_compliance
from app.services import ai_system_dependency_health as dependency_health_service
from app.services import ai_system_service_signals as service_signals
from app.constants.outcomes import (
    CONFORMANCE_UNDECLARED,
    CONFORMANCE_UNREPORTED,
    ERROR_CAUSE_LABELS,
)
from app.services.ai_system_down_rule import (
    CALLS_DEGRADED,
    CALLS_DOWN,
    DOWN_AFTER_CONSECUTIVE_FAILURES,
    VERDICT_SCORE,
    latest_calls_verdict,
)
from app.services.ai_system_outcomes import (
    RUN_DEVIATION,
    RUN_FAILED,
    RUN_NO_WORK,
    RUN_SUCCESS,
    class_counts,
    down_rule_failed,
    rate_cond,
    run_class,
    success_rate,
)
from app.services.ai_system_signal_profile import ADVISORY, NOT_APPLICABLE
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, exclusion_summary, judged_run
from app.services.assurance_inbox import AssuranceInboxService

log = logging.getLogger("brutor-assurance")

DEFAULT_PERIOD_HOURS = 720  # 30 days

#: The actor that auto-clears a superseded t_class finding.
SYSTEM_ACTOR_TCLASS = "system:tokenomics"



def _period_start(period_hours: int) -> datetime:
    if period_hours <= 0:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(hours=period_hours)


def _run_facts(
    db: Session, tenant_id: str, ai_system_id: str, since: datetime
) -> Dict[str, Any]:
    """The run facts every rate component and the report read.

    Graded over JUDGED runs only (THE exclusion predicate,
    `ai_system_judgement_exclusion`): a run inside an incident window of the
    system, or a tenant-wide one, is evidence, not a grade. It is counted in
    `excluded_runs`, never silently dropped."""
    excluded_runs = int(
        db.query(func.count(AISystemRun.id))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= since,
            ~judged_run(AISystemRun, purpose=PURPOSE_RATE),
        )
        .scalar()
        or 0
    )
    rows = (
        db.query(
            AISystemRun.terminal_state,
            AISystemRun.chain_integrity,
            AISystemRun.outcome_conformance,
            AISystemRun.outcome_counts_toward_baseline,
            AISystemRun.outcome_id,
            AISystemRun.error_cause,
            func.count(AISystemRun.id),
            func.sum(AISystemRun.total_cost_usd),
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= since,
            judged_run(AISystemRun, purpose=PURPOSE_RATE),
        )
        .group_by(
            AISystemRun.terminal_state,
            AISystemRun.chain_integrity,
            AISystemRun.outcome_conformance,
            AISystemRun.outcome_counts_toward_baseline,
            AISystemRun.outcome_id,
            AISystemRun.error_cause,
        )
        .all()
    )

    by_state: Dict[str, int] = {}
    by_class: Dict[str, int] = class_counts(())
    cost_by_class: Dict[str, float] = {c: 0.0 for c in by_class}
    outcomes: Dict[Any, Dict[str, Any]] = {}
    error_causes: Dict[str, int] = {}
    signed = 0
    asserted = 0
    external = 0
    total = 0
    cost = 0.0
    for state, integrity, conformance, counts, outcome_id, cause, count, subtotal in rows:
        count = int(count)
        cls = run_class(state, conformance, counts)
        by_state[state or "unknown"] = by_state.get(state or "unknown", 0) + count
        by_class[cls] += count
        cost_by_class[cls] += float(subtotal or 0.0)
        total += count
        if integrity == CHAIN_INTACT:
            signed += count
        elif integrity == CHAIN_CLIENT_ASSERTED:
            asserted += count
        elif integrity == CHAIN_OBSERVED_EXTERNAL:
            external += count
        cost += float(subtotal or 0.0)
        if cause:
            error_causes[cause] = error_causes.get(cause, 0) + count
        if outcome_id:
            row = outcomes.setdefault(outcome_id, {
                "outcome_id": outcome_id,
                "runs": 0,
                "conformance": conformance,
                "counts_toward_baseline": counts,
                **{c: 0 for c in by_class},
            })
            row["runs"] += count
            row[cls] += count

    successful = by_class[RUN_SUCCESS]
    return {
        "total_runs": total,
        # Runs in the period an incident window excluded from
        # every rate above (kept as evidence; listed in `exclusions`).
        "excluded_runs": excluded_runs,
        "terminal_state_mix": dict(sorted(by_state.items())),
        # THE classification (`ai_system_outcomes`): the three
        # answers per run, combined once. Replaces the client-reported
        # `client_outcomes` / `client_resolved_rate`.
        "run_classes": by_class,
        "outcomes": sorted(outcomes.values(), key=lambda r: (-r["runs"], r["outcome_id"])),
        "error_causes": dict(sorted(error_causes.items())),
        "successful_runs": successful,
        "success_rate": success_rate(by_class),
        # Empty polls: completed, declared, not work. Overhead, never in the
        # rate and never averaged into the cost of the work.
        "no_work_runs": by_class[RUN_NO_WORK],
        "no_work_cost_usd": round(cost_by_class[RUN_NO_WORK], 6),
        "deviating_runs": by_class[RUN_DEVIATION],
        "failed_runs": by_class[RUN_FAILED],
        # Coverage and provenance, separately — the same split run stats
        # reports, so the report, the health tile and the runs banner can
        # never again answer "how much did we see" with three numbers.
        "chain_integrity_pct": (signed / total) if total else None,
        "observed_pct": ((signed + asserted + external) / total) if total else None,
        "asserted_pct": (asserted / total) if total else None,
        # Ingested from external telemetry: complete as reported, with
        # no gateway control evidence. Inside observed_pct, disclosed apart.
        "external_pct": (external / total) if total else None,
        "total_cost_usd": round(cost, 6),
        # The number nobody else can give them — over the work only.
        "cost_per_processed_outcome": (
            cost_by_class[RUN_SUCCESS] / successful if successful else None
        ),
        "wasted_cost_usd": round(cost_by_class[RUN_FAILED] + cost_by_class[RUN_DEVIATION], 6),
        **_exception_sums(db, tenant_id, ai_system_id, since),
    }


def _recent_runs(
    db: Session, tenant_id: str, ai_system_id: str, since: datetime
) -> Dict[str, Any]:
    """THE down rule over the system's own latest finished runs — health's
    "is it working NOW" check beside the period rates.

    Reads the latest `DOWN_AFTER_CONSECUTIVE_FAILURES` judged, non-replay
    runs in the period that are EVIDENCE under THE run classification
    (`ai_system_outcomes`): `success` answers, `deviation` and
    `failed` do not; an empty poll (`no_work`) or an abandoned / cancelled
    run (`not_decisive`) is skipped:
    polls that kept succeeding must not hide screenings that all errored.
    Returns the verdict (`ok` | `degraded` | `down`, or None with no such
    runs) and the runs it read, newest first, with each failed run's error
    cause, so the UI and the reason can say WHY."""
    ended = func.coalesce(AISystemRun.ended_at, AISystemRun.started_at)
    rows = (
        db.query(
            AISystemRun.id,
            AISystemRun.terminal_state,
            AISystemRun.outcome_conformance,
            AISystemRun.outcome_counts_toward_baseline,
            AISystemRun.outcome_id,
            AISystemRun.error_cause,
            ended,
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= since,
            rate_cond(AISystemRun),
            judged_run(AISystemRun, purpose=PURPOSE_RATE),
        )
        .order_by(ended.desc(), AISystemRun.id.desc())
        .limit(DOWN_AFTER_CONSECUTIVE_FAILURES)
        .all()
    )
    latest = []
    for run_id, state, conformance, counts, outcome_id, cause, at in rows:
        cls = run_class(state, conformance, counts)
        latest.append({
            "run_id": run_id,
            "terminal_state": state,
            "run_class": cls,
            "outcome_id": outcome_id,
            "outcome_conformance": conformance,
            "error_cause": cause,
            "ended_at": _aware(at).isoformat() if at is not None else None,
            "failed": bool(down_rule_failed(cls)),
        })
    failed_runs = [r for r in latest if r["failed"]]
    causes: Dict[str, int] = {}
    for r in failed_runs:
        key = r["error_cause"] or (
            "outcome_" + (r["outcome_conformance"] or CONFORMANCE_UNREPORTED)
            if r["run_class"] == RUN_DEVIATION else "unknown"
        )
        causes[key] = causes.get(key, 0) + 1
    return {
        "verdict": latest_calls_verdict(r["failed"] for r in latest),
        "considered": len(latest),
        "failed": len(failed_runs),
        "failure_causes": causes,
        "rule_runs": DOWN_AFTER_CONSECUTIVE_FAILURES,
        "latest": latest,
    }


def _failure_causes_phrase(causes: Dict[str, int]) -> str:
    """"model unavailable", "model unavailable (2), tool failed (1)" — the
    error causes behind the failed runs a verdict read, most
    frequent first. Deviations name the outcome problem."""
    if not causes:
        return ""

    def label(key: str) -> str:
        if key in ERROR_CAUSE_LABELS:
            return ERROR_CAUSE_LABELS[key]
        if key == "outcome_" + CONFORMANCE_UNDECLARED:
            return "ended in an undeclared outcome"
        if key == "outcome_" + CONFORMANCE_UNREPORTED:
            return "reported no outcome"
        return "cause not recorded"

    ordered = sorted(causes.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) == 1:
        return label(ordered[0][0])
    return ", ".join(f"{label(k)} ({n})" for k, n in ordered)


def _recent_runs_reason(recent: Dict[str, Any], window_rate: Optional[float]) -> Optional[str]:
    """The operator's sentence for a recent-runs verdict that matters."""
    verdict = recent.get("verdict")
    if verdict not in (CALLS_DOWN, CALLS_DEGRADED):
        return None
    n = recent["considered"]
    latest_at = recent["latest"][0]["ended_at"] if recent["latest"] else None
    when = f" (latest {latest_at[:16].replace('T', ' ')} UTC)" if latest_at else ""
    hours = recent.get("window_hours") or DEFAULT_PERIOD_HOURS
    span = f"{hours // 24}-day" if hours % 24 == 0 else f"{hours}-hour"
    period = (
        f" Over the {span} health window {window_rate:.0%} of runs succeeded"
        " — the period rate alone would still read healthy."
        if window_rate is not None
        else ""
    )
    why = _failure_causes_phrase(recent.get("failure_causes") or {})
    why = f": {why}" if why else ""
    if verdict == CALLS_DOWN:
        return (
            f"The last {n} finished runs all failed{why}{when}: this system is failing now."
            f"{period}"
        )
    return (
        f"{recent['failed']} of the last {n} finished runs failed{why}{when}; "
        "reliability is capped until 3 runs in a row succeed."
    )


def _exception_sums(
    db: Session, tenant_id: str, ai_system_id: str, since: datetime
) -> Dict[str, int]:
    """Refusals and interpositions in the window, from the run rollups."""
    row = (
        db.query(
            func.sum(AISystemRun.guardrail_trip_count),
            func.sum(AISystemRun.guardrail_block_count),
            func.sum(AISystemRun.policy_denial_count),
            func.sum(AISystemRun.approval_request_count),
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= since,
            judged_run(AISystemRun, purpose=PURPOSE_RATE),
        )
        .first()
    )
    trips, blocks, denials, approvals = row or (0, 0, 0, 0)
    return {
        "guardrail_trips": int(trips or 0),
        "guardrail_blocks": int(blocks or 0),
        "policy_denials": int(denials or 0),
        "approval_requests": int(approvals or 0),
    }


def _baseline_section(
    db: Session, tenant_id: str, ai_system_id: str
) -> Dict[str, Any]:
    """The learned baselines — what "normal" means for this system.

    Every drift finding in the report is a comparison against these rows,
    so printing them makes the drift section self-explanatory: the reader
    sees the yardstick, not just the deviations. Only the system-wide rows
    (bucket='all', all actors) are printed in full; seasonal buckets and
    per-actor sub-baselines are counted, not dumped — they exist to
    sharpen detection, and a hundred `dow:*` rows would bury the signal.
    """
    from app.database.models.ai_system_drift import (
        AISystemBaseline,
        BASELINE_ALL_ACTORS,
        BASELINE_ALL_CALLERS,
        BASELINE_READY,
    )

    rows = (
        db.query(AISystemBaseline)
        .filter(
            AISystemBaseline.tenant_id == tenant_id,
            AISystemBaseline.ai_system_id == ai_system_id,
        )
        .order_by(AISystemBaseline.basis.asc(), AISystemBaseline.metric_key.asc())
        .all()
    )

    # A system that roots runs AND serves calls has two sets of
    # system-wide rows (basis runs / segments); both are the yardstick.
    system_wide = [
        b
        for b in rows
        if b.bucket == "all"
        and b.actor_id == BASELINE_ALL_ACTORS
        and b.caller_system_id == BASELINE_ALL_CALLERS
    ]
    seasonal = sum(1 for b in rows if b.bucket != "all")
    per_actor = sum(1 for b in rows if b.actor_id != BASELINE_ALL_ACTORS)
    per_caller = sum(1 for b in rows if b.caller_system_id != BASELINE_ALL_CALLERS)

    return {
        "note": (
            "What normal looks like for this system, learned from its own "
            "run ledger (median/MAD — run metrics are heavy-tailed). Every "
            "drift finding in this report is a comparison against these "
            "rows. A baseline still learning never alerts."
        ),
        "ready": sum(1 for b in rows if b.state == BASELINE_READY),
        "learning": sum(1 for b in rows if b.state != BASELINE_READY),
        "metrics": [
            {
                "metric_key": b.metric_key,
                "basis": b.basis,
                "state": b.state,
                "sample_count": b.sample_count,
                "median": b.median,
                "mad": b.mad,
                "p50": b.p50,
                "p90": b.p90,
                "p99": b.p99,
                "distribution": b.distribution or None,
                "window_start": (
                    b.window_start.isoformat() if b.window_start else None
                ),
                "window_end": b.window_end.isoformat() if b.window_end else None,
                "computed_at": (
                    b.computed_at.isoformat() if b.computed_at else None
                ),
            }
            for b in system_wide
        ],
        # Sharpeners, counted rather than dumped.
        "sub_baselines": {
            "seasonal": seasonal,
            "per_actor": per_actor,
            # Per-caller rows of a shared service.
            "per_caller": per_caller,
        },
    }


# ── The executive assurance layer (spec 1.2) ────────────────────────────
#
# The technical sections are strong evidence for a risk audience; what
# they do not answer in the first twenty seconds is "can I rely on this
# system right now, and why?". These helpers synthesise the sections
# into (a) five named dimensions, (b) a maturity level, and (c) one
# conclusion paragraph — all derived from facts already in the report,
# never a new judgment the reader cannot re-derive below.

ASSURANCE_LEVELS = [
    {"level": 0, "key": "observable", "label": "Observable",
     "means": "Traffic and behaviour are visible through the gateway."},
    {"level": 1, "key": "baseline_established", "label": "Baseline established",
     "means": "The system's normal operating behaviour has been learned."},
    {"level": 2, "key": "controlled", "label": "Controlled",
     "means": "An approved contract binds the system; enforcement is active."},
    {"level": 3, "key": "evidence_backed", "label": "Evidence-backed",
     "means": "Execution chains are signed and replay evidence exists."},
    {"level": 4, "key": "independently_assured", "label": "Independently assured",
     "means": "Independent external evidence is attached and unexpired."},
]

#: Signed-chain share required before execution evidence counts as strong.
CHAIN_SIGNED_FLOOR = 0.95


def _assurance_dimensions(
    *,
    runs: Dict[str, Any],
    ready_baselines: int,
    open_drift: List[AISystemDriftEvent],
    contract_compliance: Dict[str, Any],
    suite,
    last_replay,
    evidence_rows,
) -> Dict[str, Dict[str, str]]:
    """Five dimensions, three honest states each: ok | partial | missing.

    A customer reading "assured with exceptions" needs to see at a glance
    WHICH half of assurance is done — operational evidence can be complete
    while contract assurance has not even started, and one merged verdict
    hides that.
    """
    total = runs.get("total_runs", 0)
    observed = runs.get("observed_pct")
    signed = runs.get("chain_integrity_pct") or 0.0

    if total == 0:
        operational = ("missing", "No runs were observed in the period.")
    elif observed is not None and observed < 1.0:
        operational = (
            "partial",
            f"Only {observed:.0%} of runs were observed completely.",
        )
    else:
        operational = ("ok", f"{total} run(s) fully observed in the period.")

    unexplained = sum(1 for d in open_drift if not d.is_explained)
    if not ready_baselines:
        behavioural = (
            "missing",
            "Baselines are still learning; behaviour cannot be judged "
            "against normal yet.",
        )
    elif unexplained:
        behavioural = (
            "partial",
            f"{unexplained} open drift finding(s) have no attributed cause.",
        )
    elif open_drift:
        behavioural = (
            "partial",
            f"{len(open_drift)} open drift finding(s) under triage.",
        )
    else:
        behavioural = ("ok", "Behaviour matches the learned baselines.")

    envelope_eval = contract_compliance.get("envelope")
    if not contract_compliance.get("under_signature"):
        contract = (
            "missing",
            "No approved operating envelope is in force — nothing is "
            "formally assured until one is signed off.",
        )
    elif envelope_eval and envelope_eval.get("breached"):
        contract = (
            "partial",
            f"{envelope_eval['breached']} signed envelope term(s) breached.",
        )
    elif contract_compliance.get("config_drifted"):
        contract = (
            "partial",
            "The live configuration has drifted from the approved contract.",
        )
    else:
        contract = ("ok", "An approved contract is in force and met.")

    has_replay = bool(suite and last_replay)
    if signed >= CHAIN_SIGNED_FLOOR and has_replay:
        execution = (
            "ok",
            "Execution chains are signed and replay evidence exists.",
        )
    elif signed >= CHAIN_SIGNED_FLOOR or has_replay or signed > 0:
        gaps = []
        if signed < CHAIN_SIGNED_FLOOR:
            gaps.append(
                f"only {signed:.0%} of runs carry gateway-signed chains"
            )
        if not has_replay:
            gaps.append("no replay evidence")
        joined_gaps = "; ".join(gaps)
        execution = ("partial", joined_gaps[0].upper() + joined_gaps[1:] + ".")
    else:
        execution = (
            "missing",
            "No signed execution chains and no replay evidence.",
        )

    rows = evidence_rows or []
    independent = [e for e in rows if e.independent and not e.is_expired]
    if independent:
        evidence = (
            "ok",
            f"{len(independent)} independently produced, unexpired "
            "evidence item(s) attached.",
        )
    elif rows:
        evidence = (
            "partial",
            "Evidence is attached but none of it is independently "
            "produced and unexpired.",
        )
    else:
        evidence = ("missing", "No external evidence is attached.")

    return {
        key: {"status": status, "detail": detail}
        for key, (status, detail) in {
            "operational_evidence": operational,
            "behavioural_stability": behavioural,
            "contract_assurance": contract,
            "execution_evidence": execution,
            "independent_evidence": evidence,
        }.items()
    }


#: What each rung requires, and the one thing an operator does to earn it.
#:
#: The ladder was a readout: it said "you are at L1, next is L2" and stopped.
#: An operator who wants L2 then has to work out which of five dimensions is
#: blocking and which screen fixes it. That is a workflow the product knows
#: and the user does not, so the product should say it.
#:
#: `dimension` ties the rung to the dimension that gates it, so the blocker
#: text is never hand-maintained alongside the dimension logic — it is read
#: from the same evaluated dimension the ladder itself uses.
LADDER_STEPS = {
    0: {
        "dimension": "operational_evidence",
        "requires": "Runs observed through the gateway in this period.",
        "action": "Route this system's traffic through the gateway.",
        "surface": "runs",
    },
    1: {
        "dimension": "behavioural_stability",
        "requires": "Learned baselines, so behaviour can be judged against normal.",
        "action": (
            "Let baselines finish learning — they need a run history, not a "
            "configuration change."
        ),
        "surface": "drift",
    },
    2: {
        "dimension": "contract_assurance",
        "requires": "An approved operating envelope in force.",
        "action": "Declare an operating envelope and have it signed off.",
        "surface": "contract",
    },
    3: {
        "dimension": "execution_evidence",
        "requires": (
            "Gateway-signed execution chains on effectively all runs, plus "
            "replay evidence."
        ),
        "action": (
            "Have agents propagate the gateway run header so chains are "
            "signed rather than client-asserted, and bind a replay suite."
        ),
        "surface": "replay",
    },
    4: {
        "dimension": "independent_evidence",
        "requires": "Unexpired evidence produced by someone other than this platform.",
        "action": (
            "Attach an independent impact assessment, evaluation or "
            "red-team report."
        ),
        "surface": "evidence",
    },
}


def _next_step(
    next_level: int, dimensions: Dict[str, Dict[str, str]]
) -> Optional[Dict[str, Any]]:
    """How to earn the next rung — requirement, current blocker, and the
    action that closes it.

    The blocker is the evaluated dimension's own `detail`, so this can never
    drift from the logic that decided the rung was unmet. A hand-written
    blocker string would be wrong the first time the dimension rules change.
    """
    step = LADDER_STEPS.get(next_level)
    if step is None:
        return None
    dim = dimensions.get(step["dimension"]) or {}
    return {
        "dimension": step["dimension"],
        "requires": step["requires"],
        "blocker": dim.get("detail"),
        "status": dim.get("status"),
        "action": step["action"],
        "surface": step["surface"],
    }


def _assurance_level(dimensions: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """The maturity ladder — cumulative, so a level is only reached when
    every rung below it holds. "How assured is my AI system?" deserves a
    one-word answer with the criteria printed beside it."""
    ok = {k: v["status"] for k, v in dimensions.items()}
    rungs = [
        ok["operational_evidence"] != "missing",                    # L0
        ok["behavioural_stability"] != "missing",                   # L1
        ok["contract_assurance"] in ("ok", "partial"),              # L2
        ok["execution_evidence"] == "ok",                           # L3
        ok["independent_evidence"] == "ok",                         # L4
    ]
    level = -1
    for i, held in enumerate(rungs):
        if not held:
            break
        level = i

    entry = ASSURANCE_LEVELS[level] if level >= 0 else None
    next_entry = (
        ASSURANCE_LEVELS[level + 1] if level + 1 < len(ASSURANCE_LEVELS) else None
    )
    return {
        "level": level,  # -1 = not even observable in this period
        # Presentation name (spec 1.3). The number measures how much
        # EVIDENCE exists, not how trustworthy the system is — "Level 0"
        # unlabelled reads as "basically not assured" and undersells a
        # report that contains real assurance evidence.
        "scale": "Evidence maturity",
        "label": entry["label"] if entry else "Not observable",
        "means": entry["means"] if entry else
            "No traffic was observed in the period.",
        "next": (
            {
                "level": next_entry["level"],
                "label": next_entry["label"],
                "means": next_entry["means"],
                # The actionable half: what this rung requires, what is
                # currently blocking it, and the one thing to do about it.
                **(_next_step(level + 1, dimensions) or {}),
            }
            if next_entry
            else None
        ),
        "ladder": ASSURANCE_LEVELS,
        # Every rung with its requirement and whether it is held, so the UI
        # can render the ladder as a checklist rather than a single number.
        # `held` is computed from the same `rungs` list the level came from,
        # so the checklist and the headline can never disagree.
        "rungs": [
            {
                "level": i,
                "label": ASSURANCE_LEVELS[i]["label"],
                "held": bool(held),
                "requires": LADDER_STEPS[i]["requires"],
                "dimension": LADDER_STEPS[i]["dimension"],
                "status": (dimensions.get(LADDER_STEPS[i]["dimension"]) or {}).get(
                    "status"
                ),
            }
            for i, held in enumerate(rungs)
        ],
    }


#: The plain-language assurance status (spec 1.3) — the word a CIO reads
#: first. Derived from verdict × basis: "assured with exceptions" over an
#: unsigned envelope is CONDITIONAL, and saying so up front is what stops
#: the verdict enum and the evidence gaps from fighting in the reader's
#: head.
#: The label carries its basis. "Assured" on its own was read as a verdict on
#: the system; what is actually asserted is envelope conformance over
#: observed runs, and the badge is what gets screenshotted, so the badge
#: says so. Correctness is never evaluated here — see `_not_covered`.
_STATUS_LABELS = {
    "assured": "Assured · operating envelope",
    "assured_with_exceptions": "Assured with exceptions · operating envelope",
    "conditionally_assured": "Conditionally assured · observed history",
    "violations": "Not assured — contract violations",
    "insufficient_evidence": "Evidence insufficient",
}

#: One sentence under the badge, everywhere the badge appears.
_STATUS_BASIS_NOTES = {
    "assured": "Envelope conformance over observed runs. Correctness is not evaluated.",
    "assured_with_exceptions": (
        "Envelope conformance over observed runs, with exceptions listed below. "
        "Correctness is not evaluated."
    ),
    "conditionally_assured": (
        "Measured against the system's own history, not a signed envelope. "
        "Correctness is not evaluated."
    ),
    "violations": "At least one signed envelope term was breached in the period.",
    "insufficient_evidence": "Too few observed runs to evaluate the envelope.",
}


def _assurance_status(verdict: Dict[str, Any]) -> Dict[str, str]:
    status = verdict["status"]
    if status in ("assured", "assured_with_exceptions") and (
        verdict.get("basis") in ("observed_history", "reported_telemetry")
    ):
        code = "conditionally_assured"
    else:
        code = status
    return {
        "code": code,
        "label": _STATUS_LABELS[code],
        "basis_note": _STATUS_BASIS_NOTES[code],
    }


def _assurance_conclusion(
    *,
    system_name: str,
    verdict: Dict[str, Any],
    dimensions: Dict[str, Dict[str, str]],
    level: Dict[str, Any],
    runs: Dict[str, Any],
    open_drift: List[AISystemDriftEvent],
    under_signature: bool,
) -> Dict[str, Any]:
    """The executive interpretation: status, what we know, what prevents
    full assurance, recommended action — and the assembled paragraph.

    Deterministic synthesis of facts stated elsewhere in the report — the
    conclusion must never know something the sections below cannot prove.
    """
    status = verdict["status"]
    unexplained = sum(1 for d in open_drift if not d.is_explained)
    signed = runs.get("chain_integrity_pct")

    if status == "insufficient_evidence":
        opening = (
            f"{system_name} produced too little evidence in the reporting "
            "period to support any assurance conclusion."
        )
    elif status == "violations":
        opening = (
            f"{system_name} breached its approved operating envelope "
            "during the reporting period."
        )
    elif under_signature:
        opening = (
            f"{system_name} operated within its approved operating "
            "envelope during the reporting period."
        )
    else:
        opening = (
            f"{system_name} operated within its observed historical "
            "behaviour during the reporting period, but formal assurance "
            "is incomplete."
        )

    caveats = []
    if unexplained:
        caveats.append(
            f"{unexplained} unexplained behavioural drift finding(s) "
            "remain open"
        )
    elif open_drift:
        caveats.append(
            f"{len(open_drift)} behavioural drift finding(s) are under triage"
        )
    if not under_signature:
        caveats.append("the operating contract is not approved")
    if (
        signed is not None
        and signed < CHAIN_SIGNED_FLOOR
        and runs.get("total_runs", 0) > 0
    ):
        caveats.append(
            f"{1 - signed:.0%} of runs lack cryptographically signed "
            "execution chains"
        )
    if dimensions["independent_evidence"]["status"] != "ok":
        caveats.append("no independent evidence is attached")

    if caveats:
        joined = "; ".join(caveats)
        # Uppercase only the first character — .capitalize() would
        # lowercase a model or vendor name later in the sentence.
        middle = " " + joined[0].upper() + joined[1:] + "."
    else:
        middle = ""

    if status == "violations":
        action = (
            "Treat the system as outside its authorised bounds until the "
            "breaches are resolved and a new period demonstrates "
            "conformance."
        )
    elif status == "insufficient_evidence":
        action = (
            "Place no reliance on this report alone; restore evidence "
            "flow before drawing conclusions."
        )
    elif caveats:
        action = (
            "Remain under monitored operation until the outstanding "
            "assurance requirements are completed."
        )
    else:
        action = (
            "On the evidence in this report, the system may be relied on "
            "within its approved operating envelope."
        )

    status_code = _assurance_status(verdict)["code"]
    unexplained_open = sum(1 for d in open_drift if not d.is_explained)
    if status_code == "violations":
        why = (
            "A signed commitment was broken: the system did something its "
            "approvers said it must not. This is the finding assurance "
            "exists to catch, and it outranks every reassuring number in "
            "this report."
        )
    elif status_code == "insufficient_evidence":
        why = (
            "Nothing can be concluded in either direction. Absence of "
            "evidence is not safety — a quiet report is not a clean one."
        )
    elif status_code == "conditionally_assured" and (open_drift or unexplained_open):
        why = (
            "The system's behaviour has deviated from its own history, and "
            "no approved operating boundary exists to judge the deviation "
            "against. The open findings must be triaged on their own merits."
        )
    elif status_code == "conditionally_assured":
        why = (
            "No evidence of abnormal behaviour was detected during the "
            "reporting period. However, it cannot yet be established that "
            "the system remains within an approved operating boundary — "
            "behaving like itself is not the same as behaving as approved."
        )
    elif status_code == "assured_with_exceptions":
        why = (
            "The system stayed within the bounds its approvers signed off. "
            "The exceptions on record are governed events — refusals and "
            "responses the platform enforced — not silent deviations."
        )
    else:
        why = (
            "The system is doing what its approvers signed off, and the "
            "evidence in this report supports that conclusion."
        )

    return {
        "status": _assurance_status(verdict),
        "what_we_know": opening,
        "what_prevents_full_assurance": caveats,
        "recommended_action": action,
        # Spec 1.4 — the final translation: a busy reader should not have
        # to convert a list of missing evidence into "so is this system
        # okay or not?" themselves.
        "why_it_matters": why,
        # The assembled paragraph — kept for 1.2 consumers and for
        # anywhere a single sentence beats three labelled rows.
        "conclusion": opening + middle + " " + action,
    }


_REC_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _recommended_controls(
    system: ResourceGroup,
    *,
    runs: Dict[str, Any],
    contract_compliance: Dict[str, Any],
    open_drift: List[AISystemDriftEvent],
    conf,
    suite,
    expectation,
    evidence_rows,
) -> List[Dict[str, Any]]:
    """Turn the report's findings into named next controls.

    Strictly rule-based over facts already in the report — no model call,
    no judgment the reader cannot re-derive from the sections above. These
    are recommendations for the operator, never auto-applied: proposing a
    control and enacting one are different authorities.
    """
    recs: List[Dict[str, Any]] = []

    def rec(
        control: str, severity: str, reason: str, subject: Optional[str] = None
    ):
        recs.append(
            {
                "control": control,
                "severity": severity,
                "reason": reason,
                **({"subject": subject} if subject else {}),
            }
        )

    high_risk = getattr(system, "eu_ai_act_risk_tier", None) == "high"

    unexplained = [d for d in open_drift if not d.is_explained]
    if unexplained:
        rec(
            "add_response_policy",
            "high" if high_risk else "medium",
            f"{len(unexplained)} open drift finding(s) have no attributed "
            "cause. A response policy can downgrade autonomy or suspend the "
            "system automatically while an operator investigates.",
        )

    envelope_eval = contract_compliance.get("envelope")
    if envelope_eval and envelope_eval.get("insufficient"):
        rec(
            "report_run_ends",
            "medium",
            f"{envelope_eval['insufficient']} envelope term(s) could not be "
            "evaluated on this period's evidence. Report run ends from the "
            "client so every term has enough finished runs to judge.",
        )

    if not contract_compliance.get("under_signature"):
        if contract_compliance.get("unsigned_declaration"):
            rec(
                "approve_contract",
                "high",
                "An operating envelope is declared but no approved contract "
                "carries it — nothing is evaluated until it is signed off.",
            )
        else:
            rec(
                "mint_contract",
                "medium",
                "No approved contract governs this system, so there is no "
                "signed bound to assure it against.",
            )

    for client in getattr(conf, "undeclared_clients", []) or []:
        rec(
            "declare_or_block_client",
            "high" if high_risk else "medium",
            "This client called the system but is not in intended_client_labels. "
            "Declare it if legitimate; block it if not.",
            subject=client,
        )

    for caller in getattr(conf, "undeclared_client_systems", []) or []:
        rec(
            "accept_or_refuse_caller_system",
            "high" if high_risk else "medium",
            "This AI System called this one but is not in "
            "intended_client_systems. Accept it as a client if legitimate; "
            "revoke its grant if not.",
            subject=caller,
        )

    # Declared outcomes.
    for outcome_id in sorted(getattr(conf, "undeclared_outcomes", {}) or {}):
        rec(
            "declare_or_fix_outcome",
            "high",
            "Runs ended in an outcome the active contract does not declare. Declare it "
            "(and approve the contract) if it is a legitimate end; otherwise the agent "
            "ended somewhere it was never approved to — investigate.",
            subject=outcome_id,
        )
    if getattr(conf, "unreported_runs", 0):
        rec(
            "report_outcomes",
            "medium",
            f"{conf.unreported_runs} completed run(s) reported no outcome. Send "
            "X-Brutor-Outcome-Id on the run-closing call so a completed run can count "
            "as a success.",
        )
    for outcome_id in sorted(getattr(conf, "evidence_mismatches", {}) or {}):
        rec(
            "check_outcome_evidence",
            "medium",
            "Runs reporting this outcome broke its declared evidence (expect). Either "
            "the agent mislabels its runs or the declaration is wrong.",
            subject=outcome_id,
        )

    unused = getattr(conf, "unused_resources", []) or []
    if unused:
        rec(
            "revoke_unused_grants",
            "low",
            f"{len(unused)} granted resource(s) were never used in the "
            "period. Least privilege: revoke or justify them.",
        )

    if suite is None:
        rec(
            "bind_replay_suite",
            "medium",
            "No replay suite is bound, so configuration changes ship with "
            "no pre-release evidence.",
        )

    asserted = runs.get("asserted_pct") or 0.0
    integrity = runs.get("chain_integrity_pct")
    if asserted > 0 or (
        integrity is not None and integrity < 1.0 and runs.get("total_runs", 0) > 0
    ):
        rec(
            "sign_run_chains",
            "medium",
            "Not every run in the period carried a gateway-signed chain. "
            "Signing from the SDK upgrades client-asserted linkage to "
            "cryptographic evidence.",
        )

    if (runs.get("external_pct") or 0.0) > 0:
        rec(
            "route_through_gateway",
            "high",
            "Part of this system's traffic reaches the ledger as ingested "
            "telemetry. Those runs can be observed but not governed: no "
            "guardrail, policy or budget applied to them. Route the model and "
            "tool calls through the gateway to turn reported history into "
            "witnessed, enforced evidence.",
        )

    if expectation is None:
        rec(
            "configure_liveness",
            "medium",
            "No liveness expectation is set. Silence is the one failure "
            "every other signal misses — nothing fires when the system "
            "simply stops calling.",
        )

    rows = evidence_rows or []
    if high_risk and not any(
        e.kind == EVIDENCE_IMPACT_ASSESSMENT and not e.is_expired for e in rows
    ):
        rec(
            "attach_impact_assessment",
            "high",
            "This system is declared high-risk and has no unexpired impact "
            "assessment on file.",
        )
    elif rows and not any(e.independent for e in rows):
        rec(
            "attach_independent_evidence",
            "low",
            "All attached evidence is self-assessed or of unstated "
            "provenance; nothing here was independently produced.",
        )

    recs.sort(key=lambda r: (_REC_SEVERITY_RANK[r["severity"]], r["control"]))
    return recs


def _human_review_or_unavailable(
    db, *, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[str, Any]:
    """The report never lies about review evidence: if the inbox
    cannot be read the section says so explicitly instead of rendering
    zeros that would read as "nothing was flagged"."""
    try:
        section = AssuranceInboxService().human_review_section(
            db, tenant_id=tenant_id, ai_system_id=ai_system_id,
            period_hours=period_hours,
        )
        return {"available": True, **section}
    except Exception:  # noqa: BLE001
        return {"available": False}


def _obligation_evidence_or_unavailable(
    db: Session, *, tenant_id: str, ai_system_id: str
) -> Dict[str, Any]:
    """Cite the latest Evidence Report per framework.

    The Assurance Report keeps its behavioural role; this section only
    points at the obligation evidence (framework, period, statement counts
    by basis, sealed record, bundle). Recomputed and judged counts are
    carried separately, exactly as the Evidence Report states them."""
    try:
        from app.services.compliance import reports as compliance_reports

        rows = compliance_reports.latest_for_system(db, tenant_id, ai_system_id)
        cited = []
        for r in rows:
            summary = r.summary or {}
            cited.append({
                "report_id": r.id,
                "framework_id": r.framework_id,
                "cadence": r.cadence,
                "period_start": r.period_start.isoformat() if r.period_start else None,
                "period_end": r.period_end.isoformat() if r.period_end else None,
                "catalog_version": r.catalog_version,
                "obligations_in_scope": summary.get("obligations_in_scope"),
                "recomputed": summary.get("recomputed") or {},
                "judged": summary.get("judged") or {},
                "declared": summary.get("declared") or {},
                "indicator": summary.get("indicator") or {},
                "record_id": r.record_id,
                "bundle_id": r.bundle_id,
            })
        return {
            "available": True,
            "reports": cited,
            "note": (
                "Latest Evidence Report per framework. Statements are shown by basis "
                "(recomputed, judged, indicator, declared) and never summed; not legal advice."
                if cited
                else "No Evidence Report has been generated for this system yet."
            ),
        }
    except Exception:  # noqa: BLE001
        return {"available": False, "reports": []}


def build_report(
    db: Session,
    system: ResourceGroup,
    *,
    period_hours: int = DEFAULT_PERIOD_HOURS,
) -> Dict[str, Any]:
    """Assemble the report. Pure read — captures nothing."""
    tenant_id = system.tenant_id
    since = _period_start(period_hours)

    runs = _run_facts(db, tenant_id, system.id, since)

    # Tenant-scoped, not system-scoped, and deliberately so: the question
    # "is there ungoverned AI activity alongside this?" is answered by the
    # estate, and a discovered asset that nobody has routed cannot be
    # attributed to an AI System precisely because it never reached us.
    coverage = attest_coverage(db, tenant_id)
    # Declared exclusions: every incident window overlapping
    # the period (who, why, runs excluded) and every baseline epoch. The
    # excluded period is stated on coverage too: unmeasured is not 100 %.
    exclusions = exclusion_summary(db, tenant_id, system.id, since=since)

    active_contract = contracts.current_contract(db, tenant_id, system.id)
    all_contracts = (
        db.query(AISystemContract)
        .filter(
            AISystemContract.tenant_id == tenant_id,
            AISystemContract.ai_system_id == system.id,
        )
        .order_by(AISystemContract.version.desc())
        .all()
    )

    drift = (
        db.query(AISystemDriftEvent)
        .filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.ai_system_id == system.id,
            AISystemDriftEvent.detected_at >= since,
        )
        .order_by(AISystemDriftEvent.detected_at.desc())
        .all()
    )
    open_drift = [d for d in drift if d.is_open]
    # The system's own open findings; dependency findings raised on it cap
    # its health instead and are reported with the edges.
    open_own = [
        d for d in open_drift if service_signals.is_own_drift(d.metric_key)
    ]

    expectation = (
        db.query(AISystemLivenessExpectation)
        .filter(
            AISystemLivenessExpectation.tenant_id == tenant_id,
            AISystemLivenessExpectation.ai_system_id == system.id,
        )
        .first()
    )

    suite = None
    last_replay = None
    if getattr(system, "replay_suite_id", None):
        suite = (
            db.query(AISystemReplaySuite)
            .filter(
                AISystemReplaySuite.id == system.replay_suite_id,
                AISystemReplaySuite.tenant_id == tenant_id,
            )
            .first()
        )
        if suite is not None:
            last_replay = (
                db.query(AISystemReplayRun)
                .filter(
                    AISystemReplayRun.tenant_id == tenant_id,
                    AISystemReplayRun.suite_id == suite.id,
                )
                .order_by(AISystemReplayRun.started_at.desc())
                .first()
            )

    conf = conformance_service.for_system(db, system, period_hours=period_hours)

    evidence_rows = (
        db.query(AISystemEvidence)
        .filter(
            AISystemEvidence.tenant_id == tenant_id,
            AISystemEvidence.ai_system_id == system.id,
        )
        .order_by(AISystemEvidence.created_at.desc())
        .all()
    )

    health = compute_health(
        db, system, runs=runs, conformance=conf, open_drift=len(open_own)
    )
    service_facts = health.get("service")
    envelope_eval = health.get("envelope")
    # Approval holds raised inside a declared
    # window are not judged by the oversight rate; the count is the one the
    # health computation graded with (one computation), stated here and on
    # coverage beside the excluded runs.
    exclusions["excluded_approval_requests"] = int(
        (health.get("oversight") or {}).get("excluded_in_incident_windows") or 0
    )
    coverage = {**coverage, "excluded_period": excluded_period(exclusions)}

    # --- contract compliance, exceptions, changes, verdict ----------
    live_envelope = envelope_service.validate_envelope(system.operating_envelope)
    under_signature = bool(
        active_contract
        and (active_contract.resolved_closure or {}).get("operating_envelope")
    )
    contract_compliance: Dict[str, Any] = {
        "under_signature": under_signature,
        "envelope": envelope_eval,
        "config_drifted": contracts.is_config_drifted(db, system),
    }
    if live_envelope and not under_signature:
        contract_compliance["unsigned_declaration"] = (
            "An operating envelope is declared on the system but no approved "
            "contract carries it; it is not evaluated until signed off."
        )

    # A shared service's refusals happen in the calls it serves.
    refusals = service_facts if service_facts is not None else runs
    exceptions = {
        "guardrail_trips": runs.get("guardrail_trips", 0),
        "guardrail_blocks": refusals.get("guardrail_blocks", 0),
        "policy_denials": refusals.get("policy_denials", 0),
        "approval_requests": refusals.get("approval_requests", 0),
        "automatic_responses": sum(
            1 for d in drift if (d.response_taken or {}).get("actions")
        ),
        "total": (
            refusals.get("guardrail_blocks", 0)
            + refusals.get("policy_denials", 0)
            + sum(1 for d in drift if (d.response_taken or {}).get("actions"))
        ),
    }

    changes = {
        "contract_versions_minted": [
            c.version for c in all_contracts if _aware(c.created_at) >= since
        ],
        "contracts_promoted": [
            c.version for c in all_contracts if _aware(c.promoted_at) >= since
        ],
        "attributed_model_changes": sum(
            1 for d in drift if d.attributed_cause == "model_change"
        ),
        "attributed_dependency_changes": sum(
            1
            for d in drift
            if d.attributed_cause
            in ("dependency_change", "dependency_degraded", "composition_change")
        ),
        "config_drifted_now": contract_compliance["config_drifted"],
    }

    verdict = _verdict(
        runs, envelope_eval, exceptions, len(open_own),
        under_signature=under_signature,
        served_calls=service_facts["served_calls"] if service_facts else 0,
        failing_dependencies=_failing_dependencies(health.get("dependency_health") or []),
    )

    baselines_section = _baseline_section(db, tenant_id, system.id)
    dimensions = _assurance_dimensions(
        runs=runs,
        ready_baselines=baselines_section["ready"],
        open_drift=open_drift,
        contract_compliance=contract_compliance,
        suite=suite,
        last_replay=last_replay,
        evidence_rows=evidence_rows,
    )
    level = _assurance_level(dimensions)

    return {
        "spec": standards.REPORT_SPEC,
        "spec_version": standards.REPORT_SPEC_VERSION,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_hours": period_hours,
        "verdict": verdict,
        # --- the executive layer (spec 1.2): the first twenty seconds ------
        "assurance_summary": {
            **_assurance_conclusion(
                system_name=system.display_name or system.name,
                verdict=verdict,
                dimensions=dimensions,
                level=level,
                runs=runs,
                open_drift=open_drift,
                under_signature=under_signature,
            ),
            "level": level,
            "dimensions": dimensions,
        },
        "contract_compliance": contract_compliance,
        "exceptions": exceptions,
        "changes": changes,
        # --- identity ----------------------------------------------------
        "identity": {
            "ai_system_id": system.id,
            "name": system.display_name or system.name,
            "system_kind": system.system_kind,
            "owner": system.owner,
            "intended_use": system.intended_use,
            "intended_client_labels": system.intended_client_labels,
            "intended_client_systems": system.intended_client_systems,
            "eu_ai_act_risk_tier": system.eu_ai_act_risk_tier,
            "lifecycle_stage": system.lifecycle_stage,
            "autonomy_level": system.autonomy_level,
        },
        "health": health,
        # What the system served as a shared service
        # (null unless its signals are on the segment basis), and the health
        # of every declared dependency: tested for shared services, assumed
        # for external agents. The same entries the health endpoint shows.
        "signal_basis": health.get("signal_basis"),
        "service": service_facts,
        "dependency_health": health.get("dependency_health") or [],
        # Report spec 1.6, additive — "Composition and
        # dependencies": each edge with its role, current status (the SAME
        # dependency-health entries above), evidence basis, contract
        # version, supplier and separability claim, plus the open
        # composition findings. Inside the frozen report, so the digest and
        # signature cover it like every other section.
        "composition": composite_compliance.composition_report_section(
            db, system, health.get("dependency_health") or []
        ),
        # The human-review loop as evidence: raised/reviewed/open
        # counts, disposition mix, time-to-disposition, and review-SLA
        # status from the assurance inbox. The line no analytics product
        # can produce: someone accountable looked, and here is the trail.
        "human_review": _human_review_or_unavailable(
            db, tenant_id=tenant_id, ai_system_id=system.id,
            period_hours=period_hours,
        ),
        # Obligation evidence: cites the latest Evidence
        # Report per framework (spec 1.5, additive).
        "obligation_evidence": _obligation_evidence_or_unavailable(
            db, tenant_id=tenant_id, ai_system_id=system.id,
        ),
        # --- G1 bounded ---------------------------------------------------
        "bounded": {
            "active_contract": (
                {
                    "version": active_contract.version,
                    "hash": active_contract.contract_hash,
                    "approved_by": active_contract.approved_by,
                    "promoted_at": (
                        active_contract.promoted_at.isoformat()
                        if active_contract.promoted_at
                        else None
                    ),
                }
                if active_contract
                else None
            ),
            "contract_history": [
                {
                    "version": c.version,
                    "hash": c.short_hash,
                    "status": c.status,
                    "approved_by": c.approved_by,
                    "widened": bool(
                        (c.diff_summary or {}).get("has_widened_permissions")
                    ),
                }
                for c in all_contracts
            ],
            "config_drifted": contracts.is_config_drifted(db, system),
        },
        # --- the yardstick the drift section is measured against ----------
        "baselines": baselines_section,
        # --- G2 non-drifting ---------------------------------------------
        "non_drifting": {
            "events": [
                {
                    "detected_at": (
                        d.detected_at.isoformat() if d.detected_at else None
                    ),
                    "drift_class": d.drift_class,
                    "metric_key": d.metric_key,
                    # Which population the baseline described.
                    "basis": d.basis,
                    "caller_system_id": d.caller_system_id,
                    "severity": d.severity,
                    # Spec 1.2 — the finding as an observable fact, not a
                    # label: what normal was, what was seen, which way it
                    # moved. "Drift in what?" must be answerable from the
                    # report alone.
                    "baseline_value": d.baseline_value,
                    "observed_value": d.observed_value,
                    "effect_size": d.effect_size,
                    "direction": d.direction,
                    "attributed_cause": d.attributed_cause,
                    "status": d.status,
                    # How it was closed (null while open).
                    "resolution_kind": d.resolution_kind,
                    "response_taken": bool((d.response_taken or {}).get("actions")),
                }
                for d in drift
            ],
            "open_count": len(open_drift),
            "unexplained_open": sum(
                1 for d in open_drift if not d.is_explained
            ),
        },
        # --- G3 doing its job ----------------------------------------------
        "doing_its_job": {
            "runs": runs,
            "replay": (
                {
                    "suite": suite.name,
                    "replayable": suite.replayable,
                    "pass_threshold": suite.pass_threshold,
                    "last_run": (
                        {
                            "pass_rate": last_replay.pass_rate,
                            "critical_outcome_changed": (
                                last_replay.critical_outcome_changed
                            ),
                            "status": last_replay.status,
                            "finished_at": (
                                last_replay.finished_at.isoformat()
                                if last_replay.finished_at
                                else None
                            ),
                        }
                        if last_replay
                        else None
                    ),
                }
                if suite
                else None
            ),
        },
        # --- G4 alive ------------------------------------------------------
        "alive": {
            "expectation": (
                {
                    "mode": expectation.mode,
                    "enabled": expectation.enabled,
                    "window_seconds": expectation.window_seconds,
                }
                if expectation
                else None
            ),
            "last_run_at": _last_run_at(db, tenant_id, system.id),
            # The same verdict the panel, the scorer and the
            # list read; the report never re-derives it.
            **{
                k: v
                for k, v in liveness_state(db, system, window_hours=period_hours)
                .to_dict()
                .items()
                if k in ("state", "reason", "inbound_calls", "last_activity_at")
            },
        },
        # --- conformance ----------------------------------------------------
        "conformance": conf.to_dict(),
        # --- external evidence ----------------------------------------------
        # Everything else in this report is platform-generated. This section
        # is the evidence produced *outside* the platform — impact
        # assessments, evaluations, red-team and pen-test reports — recorded
        # as verifiable pointers (uri + sha256), never as copies.
        "evidence": [
            {
                "kind": e.kind,
                "title": e.title,
                "assessor": e.assessor,
                "independent": e.independent,
                "produced_at": e.produced_at.isoformat() if e.produced_at else None,
                "valid_until": e.valid_until.isoformat() if e.valid_until else None,
                "is_expired": e.is_expired,
                "uri": e.uri,
                "sha256": e.sha256,
            }
            for e in evidence_rows
        ],
        # --- findings turned into named next controls ------------------------
        "recommended_controls": _recommended_controls(
            system,
            runs=runs,
            contract_compliance=contract_compliance,
            open_drift=open_drift,
            conf=conf,
            suite=suite,
            expectation=expectation,
            evidence_rows=evidence_rows,
        ),
        # --- report spec 1.8, additive: what was excluded from judgement -------
        # Every incident window and baseline epoch, sealed with the rest of
        # the report. Runs inside a window are recorded and sealed evidence;
        # they are listed here, never hidden.
        "judgement_exclusions": exclusions,
        # --- the population proof under all of the above ---------------------
        # Every section above answers "did the traffic we saw behave?".
        # This one answers the prior question: did we see the traffic?
        "coverage": coverage,
        # --- the section that makes the rest credible ------------------------
        "not_covered": _not_covered(
            runs, suite, conf, system, evidence_rows, coverage,
            retention=_retention_gaps(db, tenant_id),
            service=service_facts,
            dependencies=health.get("dependency_health") or [],
            exclusions=exclusions,
        ),
    }


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _aware(dt: Optional[datetime]) -> datetime:
    """Normalize for comparison: None → epoch, naive (SQLite) → UTC."""
    if dt is None:
        return _EPOCH
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _verdict(
    runs: Dict[str, Any],
    envelope_eval: Optional[Dict[str, Any]],
    exceptions: Dict[str, int],
    open_drift: int,
    *,
    under_signature: bool = False,
    served_calls: int = 0,
    failing_dependencies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """The period verdict, worst-first.

    Precedence: violations > insufficient_evidence > assured_with_exceptions
    > assured. A violation observed through thin evidence is still a
    violation — thin evidence weakens an *assured* claim, never a breach.
    And `assured` requires evidence: a silent period is not a clean one.

    Spec 1.2: the verdict also carries its **basis**. "Assured with
    exceptions" over an unsigned envelope reads as a contradiction the
    moment a customer notices nothing was approved — so a verdict earned
    only against the system's own observed history says so explicitly
    (`basis: observed_history`, conclusion amended) instead of borrowing
    the authority of a signature that does not exist.
    """
    # A shared service is evidenced by the calls it served;
    # "it has no runs of its own" is a fact about the ledger, not a verdict.
    total = runs.get("total_runs", 0) + served_calls
    failing_dependencies = list(failing_dependencies or [])
    breached = envelope_eval["breached"] if envelope_eval else 0
    insufficient_terms = envelope_eval["insufficient"] if envelope_eval else 0
    exception_total = exceptions.get("total", 0)

    if breached:
        status = "violations"
        conclusion = (
            f"The system breached {breached} term(s) of its approved "
            f"operating envelope during the reporting period."
        )
    elif total == 0:
        status = "insufficient_evidence"
        conclusion = (
            "No runs or served calls were observed in the reporting period; "
            "nothing here is evidence that the system operated within its "
            "envelope."
        )
    elif (
        envelope_eval
        and insufficient_terms == len(envelope_eval["terms"])
        and not served_calls
    ):
        status = "insufficient_evidence"
        conclusion = (
            "Too few runs to evaluate any declared envelope term; the "
            "envelope is neither met nor breached on this evidence."
        )
    elif (
        exception_total > 0 or open_drift > 0 or insufficient_terms > 0
        or failing_dependencies
    ):
        status = "assured_with_exceptions"
        parts = []
        if failing_dependencies:
            parts.append(
                "critical dependency not healthy: " + ", ".join(failing_dependencies)
            )
        if exception_total:
            parts.append(f"{exception_total} governed refusal(s)/response(s)")
        if open_drift:
            parts.append(f"{open_drift} open drift finding(s)")
        if insufficient_terms:
            parts.append(f"{insufficient_terms} envelope term(s) unevaluable")
        subject = (
            "its approved operating envelope"
            if under_signature
            else "its observed historical behaviour"
        )
        conclusion = (
            f"The system remained within {subject} "
            "during the reporting period, with exceptions on record: "
            + "; ".join(parts)
            + "."
        )
    else:
        status = "assured"
        conclusion = (
            "The system remained within its approved operating envelope "
            "during the reporting period."
            if under_signature
            else (
                "The system remained within its observed historical "
                "behaviour during the reporting period, with no exceptions."
            )
        )

    if not under_signature and status in ("assured", "assured_with_exceptions"):
        conclusion += (
            " No approved operating envelope was in force: this verdict is "
            "measured against the system's own history, not a signed bound "
            "— formal assurance is incomplete."
        )

    # A period evidenced ONLY by ingested telemetry cannot be assured
    # against an envelope, signed or not: the gateway witnessed nothing and
    # applied no control, so the most it can say is that the reported
    # history stayed inside the reported norm. The basis says so, which
    # caps the badge at "conditionally assured" through `_assurance_status`.
    external = runs.get("external_pct") or 0.0
    reported_only = total > 0 and external >= 1.0
    if reported_only and status in ("assured", "assured_with_exceptions"):
        conclusion += (
            " Every run in the period was ingested from external telemetry: "
            "the gateway witnessed none of them and enforced nothing, so this "
            "verdict rests on the exporter's account of the system's behaviour."
        )
    basis = (
        "reported_telemetry"
        if reported_only
        else ("signed_envelope" if under_signature else "observed_history")
    )

    return {
        "status": status,
        "conclusion": conclusion,
        "basis": basis,
        "envelope_terms_breached": breached,
        "envelope_terms_insufficient": insufficient_terms,
        "exceptions_total": exception_total,
        "open_drift_findings": open_drift,
        "failing_dependencies": failing_dependencies,
    }


def _last_run_dt(
    db: Session, tenant_id: str, ai_system_id: str
) -> Optional[datetime]:
    return (
        db.query(func.max(AISystemRun.started_at))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
        )
        .scalar()
    )


def _last_run_at(db: Session, tenant_id: str, ai_system_id: str) -> Optional[str]:
    value = _last_run_dt(db, tenant_id, ai_system_id)
    return value.isoformat() if value else None


def _retention_gaps(db: Session, tenant_id: str) -> List[str]:
    """How deep the ledger is. A report leaning on a 90-day ledger says so."""
    from app.database.models.tenant import Tenant
    from app.services.data_retention.policy import retention_disclosure

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    return retention_disclosure(tenant) if tenant is not None else []


def excluded_period(exclusions: Dict[str, Any]) -> Dict[str, Any]:
    """The coverage block's statement of what was NOT judged."""
    windows = exclusions.get("incident_windows") or []
    hours = exclusions.get("excluded_hours") or 0.0
    runs = exclusions.get("excluded_runs") or 0
    holds = exclusions.get("excluded_approval_requests") or 0
    if not windows:
        statement = "No incident window was declared in this period; every run was judged."
    else:
        held = (
            f" and {holds} approval request(s) raised inside them" if holds else ""
        )
        statement = (
            f"{len(windows)} declared incident window(s) covering {hours:g} h of this "
            f"period; {runs} run(s){held} were recorded and sealed but excluded "
            "from judgement — that period is unmeasured, not passed."
        )
    return {
        "incident_windows": len(windows),
        "excluded_hours": hours,
        "excluded_runs": runs,
        "excluded_approval_requests": holds,
        "statement": statement,
    }


def _not_covered(
    runs, suite, conf, system=None, evidence_rows=None, coverage=None, retention=None,
    service=None, dependencies=None, exclusions=None,
) -> List[str]:
    """What this report does **not** evidence.

    Written from the actual state rather than boilerplate, so it shrinks as
    coverage improves and an operator can watch it shrink.
    """
    gaps: List[str] = []
    # Coverage first: a gap in the *population* outranks any gap in what we
    # measured about it. If the estate is unmeasured, that is the reader's
    # first question and everything below is scoped by the answer.
    if coverage is not None:
        gaps.extend(coverage_gaps(coverage))
    # A declared exclusion is a hole in what was judged, stated
    # as one: the runs exist and are sealed, but no verdict rests on them.
    if exclusions and exclusions.get("incident_windows"):
        gaps.append(excluded_period(exclusions)["statement"])
    # Retention next: the window bounds every claim about history below.
    if retention:
        gaps.extend(retention)
    # An excluded system says so on its own report, or the estate
    # view and the system view would tell different stories.
    if system is not None:
        from app.api.v1.endpoints.admin.mission_control_cards import effective_exclusion

        excl = effective_exclusion(system)
        if excl is not None:
            until = f" until {excl['expires_at']}" if excl.get("expires_at") else ""
            gaps.append(
                f"This system is excluded from the estate health and verdict "
                f"rollup ({excl.get('reason')}{until}); its verdict here does not "
                "contribute to Mission Control's fleet judgement."
            )
    # Then the profile. Which signals were relaxed by this system's declared
    # kind scopes every verdict below, and stating it is what stops kind
    # selection being a quiet way to drop an inconvenient signal.
    if system is not None:
        gaps.extend(profile_gaps(getattr(system, "system_kind", None)))
    gaps += [
        "Behavioural change is detected against this system's own history; "
        "correctness is not evaluated. There is no ground truth at the proxy.",
        conf.scope_note,
    ]

    observed = runs.get("observed_pct")
    if observed is not None and observed < 1.0:
        gaps.append(
            f"Only {observed:.0%} of runs in this window were observed "
            f"completely; the remainder are partial or broken records."
        )
    asserted = runs.get("asserted_pct") or 0.0
    if asserted > 0:
        gaps.append(
            f"{asserted:.0%} of observed runs were grouped by a client-asserted "
            f"run id rather than a gateway-signed chain. Coverage is complete "
            f"for these runs; the step linkage rests on the client's word, "
            f"bound to its credential."
        )
    external = runs.get("external_pct") or 0.0
    if external > 0:
        gaps.append(
            f"{external:.0%} of observed runs were ingested from external "
            f"telemetry (OpenTelemetry GenAI spans) rather than proxied. They "
            f"are complete as reported by the exporter; the gateway did not "
            f"witness them, applied no control, and holds no prompt, response "
            f"or cost evidence for them."
        )
    served = (service or {}).get("served_calls", 0)
    if served:
        # Say what the evidence IS for a shared service.
        gaps.append(
            f"This system roots no runs of its own; it is assured as a shared "
            f"service from the {served} call(s) it served inside other systems' "
            f"runs. Its behaviour is judged per served call, not per task."
        )
    elif runs.get("total_runs", 0) == 0:
        gaps.append(
            "No runs in this window, so nothing here is evidence of behaviour."
        )
    for dep in dependencies or []:
        if dep.get("basis") == "assumed":
            gaps.append(
                f"Dependency {dep.get('name')} is an external agent: assumed, not "
                f"tested. Brutor sees only the gateway boundary (calls, errors, "
                f"latency); the declared assumptions stand in for evidence."
            )

    if suite is None:
        gaps.append(
            "No replay suite is bound, so no pre-release evidence exists for "
            "configuration changes."
        )
    else:
        gaps.append(
            "Replay re-evaluates authorization decisions, not agent behaviour, "
            "and does not re-run content guardrails."
        )

    rows = evidence_rows or []
    if not rows:
        gaps.append(
            "No external evidence is attached (impact assessment, evaluation, "
            "red-team or penetration-test report); everything in this report "
            "is platform-generated."
        )
    else:
        if not any(e.independent for e in rows):
            gaps.append(
                "No attached evidence is marked as independently produced; "
                "all of it is self-assessed or of unstated provenance."
            )
        live_ia = [
            e
            for e in rows
            if e.kind == EVIDENCE_IMPACT_ASSESSMENT and not e.is_expired
        ]
        if (
            system is not None
            and getattr(system, "eu_ai_act_risk_tier", None) == "high"
            and not live_ia
        ):
            gaps.append(
                "This system is declared high-risk and has no unexpired "
                "impact assessment on file."
            )

    return gaps


def compute_health(
    db: Session,
    system: ResourceGroup,
    *,
    runs: Optional[Dict[str, Any]] = None,
    conformance=None,
    open_drift: Optional[int] = None,
    period_hours: int = DEFAULT_PERIOD_HOURS,
) -> Dict[str, Any]:
    """Score the system now. Gathers whatever the caller did not pass in.

    `open_drift` is the system's OWN open drift (every basis, excluding the
    dependency findings raised on it — those cap the state instead).
    """
    tenant_id = system.tenant_id
    since = _period_start(period_hours)
    if runs is None:
        runs = _run_facts(db, tenant_id, system.id, since)
    if conformance is None:
        conformance = conformance_service.for_system(
            db, system, period_hours=period_hours
        )
    if open_drift is None:
        open_drift = service_signals.open_own_drift(db, tenant_id, system.id)

    # A shared service (no own runs, served calls) is scored
    # from its segments; everything below reads the one context.
    ctx = service_signals.signal_context(db, tenant_id, system.id, since)
    service_facts = (
        service_signals.segment_facts(db, tenant_id, system.id, since)
        if ctx.is_service_basis
        else None
    )
    ready_baselines = service_signals.ready_baselines(db, tenant_id, system.id, ctx.basis)

    if service_facts is not None:
        total = service_facts["served_calls"]
        success_rate = service_signals.segment_reliability(service_facts)
        base_cost = service_signals.segment_cost_score(db, tenant_id, system.id)
    else:
        total = runs.get("total_runs", 0)
        success_rate = runs.get("success_rate")
        base_cost = None
    liveness = liveness_state(db, system, window_hours=period_hours)

    # The operating envelope gives cost (and reliability) a DECLARED
    # target, evaluated against the terms in the ACTIVE contract: assurance
    # is against what was signed off, not an edit nobody has approved yet.
    active_contract = contracts.current_contract(db, tenant_id, system.id)
    contract_envelope = (
        (active_contract.resolved_closure or {}).get("operating_envelope")
        if active_contract
        else None
    )
    envelope_eval = (
        envelope_service.evaluate_envelope(
            db, system, period_hours=period_hours, envelope=contract_envelope
        )
        if contract_envelope
        else None
    )

    cost_component = base_cost
    reliability_component = success_rate
    if envelope_eval:
        by_signal: Dict[str, list] = {"cost": [], "reliability": []}
        for term in envelope_eval["terms"]:
            signal = envelope_service.term_spec(term["term"])["signal"]
            by_signal.setdefault(signal, []).append(term)

        def _component(terms, baseline):
            # A term that is enforced and not graded says nothing either way.
            terms = [t for t in terms if t["status"] != envelope_service.STATUS_NOT_GRADED]
            breached = [t for t in terms if t["status"] == envelope_service.STATUS_BREACHED]
            if breached:
                floor = 0.0 if any(t["severity"] == "high" for t in breached) else 0.5
                return floor if baseline is None else min(baseline, floor)
            if terms and all(t["status"] == envelope_service.STATUS_MET for t in terms):
                return 1.0 if baseline is None else baseline
            return baseline  # insufficient evidence degrades nothing

        cost_component = _component(by_signal["cost"], base_cost)
        reliability_component = _component(by_signal["reliability"], success_rate)

    # 2026-10-25 — the period rate cannot see an outage that began an hour
    # ago, so the latest finished runs are judged by THE down rule too, and
    # cap reliability: worst-of then carries it into the score. A shared
    # service is judged on the calls it serves (its liveness already applies
    # the rule to them), so it gets no own-runs verdict.
    recent = None
    if service_facts is None:
        recent = _recent_runs(db, tenant_id, system.id, since)
        recent["window_success_rate"] = success_rate
        recent["window_hours"] = period_hours
        cap = VERDICT_SCORE.get(recent["verdict"]) if recent["verdict"] else None
        if cap is not None and cap < 1.0:
            reliability_component = (
                cap if reliability_component is None else min(reliability_component, cap)
            )

    # The human control loop. Scoped to the system's own group;
    # approvals are recorded against the requesting group.
    oversight_facts = oversight_service.gather_oversight(
        db, tenant_id, [system.id], _period_start(period_hours)
    )
    oversight_component = oversight_service.score_oversight(oversight_facts)
    if ctx.oversight_not_applicable:
        oversight_component = None

    # Dependency health, the new worst-of input.
    dependencies = dependency_health_service.dependency_health(db, system)
    dep_cap = dependency_health_service.dependency_cap(dependencies)

    # Kind-aware profile. `system_kind` has always been documented as
    # selecting the signal profile; this is where it finally
    # does. Signals the kind does not enforce are computed and displayed but
    # kept out of the worst-of verdict, and every such relaxation is
    # disclosed below — a profile that quietly stopped scoring a signal
    # would be a way to launder a bad score by picking a kind.
    # Human-review debt (assurance inbox). Read-only aggregate;
    # FAIL-OPEN to `unmeasured`: health scoring must survive an inbox that
    # cannot be read (and test harnesses that never created its table).
    try:
        review_component = AssuranceInboxService().review_score(
            db, tenant_id=tenant_id, ai_system_id=system.id
        )
    except Exception:  # noqa: BLE001
        review_component = None

    profiled = apply_profile(
        getattr(system, "system_kind", None),
        {
            # The ONE liveness verdict, the same value the
            # liveness panel and the AI Systems list read. It used to be
            # "1.0 if the system has own runs in the window", which scored a
            # shared service (called only inside its callers' runs) as dead.
            "liveness": liveness.score,
            "behaviour": (
                None if not ready_baselines else (1.0 if open_drift == 0 else 0.5)
            ),
            "reliability": reliability_component,
            # Without an envelope, cost has no absolute target — a system is
            # not unhealthy for being expensive, only for becoming *more*
            # expensive, which is drift. With one, the declared bound IS the
            # target.
            "cost": cost_component,
            "conformance": conformance.score,
            # `None` when no gate was exercised; degrades nothing.
            "oversight": oversight_component,
            # Review debt from the assurance inbox. `None` until
            # the system has ever had an inbox item.
            "review": review_component,
        },
    )
    # The signal levels this system is scored under NOW — the kind's
    # profile, with oversight not applicable for a service that raised no
    # hold (never "0 %"). The health endpoint reads the same function.
    levels = service_signals.signal_profile(getattr(system, "system_kind", None), ctx)
    if levels.get("oversight") == NOT_APPLICABLE:
        profiled["enforced"].pop("oversight", None)
        profiled["display"]["oversight"] = {"score": None, "level": NOT_APPLICABLE}
        profiled["profile"] = levels
    enforced = profiled["enforced"]
    # If liveness is not enforced for this kind, silence must not override
    # the verdict either. Relaxing the score while still forcing the SILENT
    # state would make the relaxation cosmetic — a quiet assistant would
    # still read as an outage, which is the false alarm the profile exists
    # to remove.
    liveness_counts = "liveness" in enforced

    result = score_health(
        liveness=enforced.get("liveness"),
        behaviour=enforced.get("behaviour"),
        reliability=enforced.get("reliability"),
        cost=enforced.get("cost"),
        conformance=enforced.get("conformance"),
        oversight=enforced.get("oversight"),
        review=enforced.get("review"),
        # The scorer's integrity floor is an EVIDENCE-SUFFICIENCY gate ("did
        # we see enough to judge?"), so it takes coverage — intact plus
        # client-asserted — not the signed-only share. Feeding it the signed
        # share would flip a system to `unknown` merely because its agents
        # correlate runs by id, punishing the documented integration path.
        # Provenance (signed vs asserted) is disclosed in the report instead.
        chain_integrity_pct=runs.get("observed_pct"),
        is_suspended=(system.autonomy_level == "suspended"),
        # Silent = the one liveness function says so. Silence
        # beats learning, and a card that reads "Learning" while the liveness
        # panel reads "Silent" — or the reverse — is exactly the two-answers
        # bug that function exists to end. idle / unstarted / not_configured
        # are never silent.
        is_silent=liveness_counts and liveness.is_silent,
        is_learning=(ready_baselines == 0 and total > 0),
        open_drift_events=open_drift,
        dependency_cap=dep_cap,
        dependency_reasons=dependency_health_service.cap_reasons(dependencies),
        recent_runs_verdict=recent["verdict"] if recent else None,
        recent_runs_reason=_recent_runs_reason(recent, success_rate) if recent else None,
    )
    # Advisory signals are "computed and displayed, excluded from the
    # verdict" (signal profile). The verdict above used enforced values
    # only; the stored components carry the advisory values too, or the
    # panel reads "not measured" for a signal that was measured — e.g. a
    # shared service's liveness (advisory for kind `service`).
    for sig, shown in profiled["display"].items():
        if shown.get("level") == ADVISORY:
            result[f"{sig}_score"] = shown.get("score")
    result["runs_24h"] = runs.get("total_runs", 0)
    result["signal_basis"] = ctx.basis
    result["signal_context"] = ctx.to_dict()
    result["service"] = service_facts
    result["dependency_health"] = dependencies
    result["dependency_cap"] = dep_cap
    result["recent_runs"] = recent
    result["open_drift_events"] = open_drift
    # The profile travels with the score: the UI needs the per-signal level
    # to render an advisory signal differently from an enforced one, and
    # `not_applicable` as "n/a" rather than as a zero or a green tick.
    result["signal_profile"] = profiled["profile"]
    result["signal_levels"] = profiled["display"]
    result["profile_relaxations"] = profiled["relaxations"]
    result["baseline_state"] = "ready" if ready_baselines else "learning"
    result["envelope"] = envelope_eval
    # The facts travel with the score so the report and the card read
    # the same numbers, and so an operator can see *why* oversight scored
    # what it did without a second query.
    result["oversight"] = oversight_service.oversight_report_section(
        oversight_facts
    )
    result["reasons"] = list(result.get("reasons") or []) + (
        oversight_service.oversight_reasons(oversight_facts)
    )

    # The pinned card judgments, computed with the same inputs the
    # report uses so a card chip and a report verdict can never disagree —
    # including the automatic-responses term, which is one cheap count.
    responses_taken = (
        db.query(func.count(AISystemDriftEvent.id))
        .filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.ai_system_id == system.id,
            AISystemDriftEvent.detected_at >= _period_start(period_hours),
            AISystemDriftEvent.response_taken.isnot(None),
        )
        .scalar()
        or 0
    )
    refusals = service_facts if service_facts is not None else runs
    exceptions_total = (
        refusals.get("guardrail_blocks", 0)
        + refusals.get("policy_denials", 0)
        + responses_taken
    )
    result["verdict"] = _verdict(
        runs,
        envelope_eval,
        {"total": exceptions_total},
        open_drift,
        served_calls=service_facts["served_calls"] if service_facts else 0,
        failing_dependencies=_failing_dependencies(dependencies),
    )
    return result


def _failing_dependencies(entries: List[Dict[str, Any]]) -> List[str]:
    """Names of the critical dependencies capping the verdict."""
    return [d["name"] for d in entries if d.get("caps_root")]


#: How far back a candidate cause may sit and still explain a workload
#: class transition. The class is a 30-day median, so it moves slowly —
#: a change event inside this window is a plausible driver, one outside
#: it is not.
CLASS_CHANGE_CAUSE_WINDOW_HOURS = 72


def _class_change_cause(
    db: Session, system: ResourceGroup
) -> tuple[str, Dict[str, Any]]:
    """Cheap, honest rungs of the cause ladder for a class transition.

    Only the causes the control plane can check from its own tables are
    attempted (contract change, composition change); everything else is
    `unexplained` — rung 8 says so plainly rather than dressing up a
    guess. The full statistical ladder lives in the core's drift sweep
    and is not imitated here.
    """
    from app.database.models.ai_system_drift import (
        CAUSE_COMPOSITION_CHANGE,
        CAUSE_CONTRACT_CHANGE,
        CAUSE_UNEXPLAINED,
    )
    from app.database.models.resource_group import (
        resource_group_llm_models,
        resource_group_server_configs,
    )

    since = datetime.now(timezone.utc) - timedelta(
        hours=CLASS_CHANGE_CAUSE_WINDOW_HOURS
    )

    contract = (
        db.query(AISystemContract)
        .filter(
            AISystemContract.tenant_id == system.tenant_id,
            AISystemContract.ai_system_id == system.id,
            AISystemContract.created_at >= since,
        )
        .order_by(AISystemContract.created_at.desc())
        .first()
    )
    if contract is not None:
        return CAUSE_CONTRACT_CHANGE, {
            "contract_version": contract.version,
            "contract_created_at": contract.created_at.isoformat(),
        }

    for table, label in (
        (resource_group_llm_models, "llm model binding"),
        (resource_group_server_configs, "mcp server binding"),
    ):
        changed = (
            db.query(func.count())
            .select_from(table)
            .filter(
                table.c.resource_group_id == system.id,
                table.c.created_at >= since,
            )
            .scalar()
            or 0
        )
        if changed:
            return CAUSE_COMPOSITION_CHANGE, {
                "changed": label,
                "bindings_added": int(changed),
            }

    return CAUSE_UNEXPLAINED, {}


def _emit_class_change(
    db: Session,
    system: ResourceGroup,
    old_class: Optional[str],
    new_class: Optional[str],
    workload: Dict[str, Any],
) -> None:
    """Cost-complexity drift: the workload's Big-T class moved.

    A class transition is a change in how cost GROWS, which matters more
    than any single bill — so it lands in the same findings list as
    behavioural drift and liveness. Honesty rules:

      * first classification (None → class) is evidence arriving, not a
        change — no finding;
      * class → None is evidence thinning below MIN_RUNS_FOR_CLASS, also
        not a change;
      * moving DOWN the ladder is still a structural change worth a row,
        but at low severity — good news is not an alarm;
      * an open t_class finding for the same arrival class is a standing
        condition, not a new event (same dedup rule as envelope terms).
    """
    if old_class is None or new_class is None or old_class == new_class:
        return

    from app.database.models.ai_system_drift import DRIFT_COST
    from app.services.tokenomics import T_CLASS_RANK

    old_rank = T_CLASS_RANK.get(old_class)
    new_rank = T_CLASS_RANK.get(new_class)
    if old_rank is None or new_rank is None:
        return
    moved_up = new_rank > old_rank

    standing = (
        db.query(AISystemDriftEvent)
        .filter(
            AISystemDriftEvent.tenant_id == system.tenant_id,
            AISystemDriftEvent.ai_system_id == system.id,
            AISystemDriftEvent.metric_key == "t_class",
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
        .first()
    )
    if standing is not None:
        if (standing.cause_evidence or {}).get("to_class") == new_class:
            return
        # A different arrival class supersedes the standing finding — the
        # operator triages the current state, not a stale intermediate.
        from app.services.drift_resolution import auto_clear

        auto_clear(
            standing,
            by=SYSTEM_ACTOR_TCLASS,
            note=f"Superseded by a newer arrival-class finding ({new_class}).",
        )

    cause, cause_detail = (
        _class_change_cause(db, system) if moved_up else ("unexplained", {})
    )
    # A downward move needs no cause hunt: it is the operator's levers
    # working. Record it plainly.
    if not moved_up:
        from app.database.models.ai_system_drift import CAUSE_UNEXPLAINED
        cause = CAUSE_UNEXPLAINED

    severity = (
        "high"
        if moved_up and new_class == "T(n·k·a)"
        else "medium" if moved_up else "low"
    )

    db.add(
        AISystemDriftEvent(
            id=f"drift-{ULID()}",
            tenant_id=system.tenant_id,
            ai_system_id=system.id,
            drift_class=DRIFT_COST,
            metric_key="t_class",
            baseline_value=float(old_rank),
            observed_value=float(new_rank),
            direction="up" if moved_up else "down",
            severity=severity,
            attributed_cause=cause,
            cause_evidence={
                "summary": (
                    f"Workload class changed {old_class} → {new_class}"
                ),
                "from_class": old_class,
                "to_class": new_class,
                "k_median": workload.get("k_median"),
                "a_max": workload.get("a_max"),
                "delegation_share": workload.get("delegation_share"),
                "cache_share": workload.get("cache_share"),
                "runs": workload.get("runs"),
                **cause_detail,
            },
        )
    )


def upsert_health(db: Session, system: ResourceGroup) -> AISystemHealth:
    """Materialise the score so tiles do not recompute it per render."""
    scored = compute_health(db, system)

    # A breached envelope term becomes an open `contract` finding on
    # the same tick that scored it, so the response loop can act on it. The
    # emitter dedupes standing breaches and auto-resolves healed ones.
    envelope_service.emit_envelope_violations(db, system, scored.get("envelope"))
    # A failing critical dependency (or a degradable one
    # whose fallback did not hold) becomes an open `dependency` finding on
    # this root, which the closed loop's response policies can target; it
    # resolves itself when the dependency recovers.
    dependency_health_service.emit_dependency_findings(
        db, system, scored.get("dependency_health") or []
    )

    row = (
        db.query(AISystemHealth)
        .filter(
            AISystemHealth.tenant_id == system.tenant_id,
            AISystemHealth.ai_system_id == system.id,
        )
        .first()
    )
    if row is None:
        row = AISystemHealth(
            id=f"hlt-{ULID()}",
            tenant_id=system.tenant_id,
            ai_system_id=system.id,
        )
        db.add(row)

    # Pinned card judgments, one row per system for the dashboard.
    from app.services.tokenomics import classify_workload

    workload = classify_workload(db, system.tenant_id, system.id)
    # Cost-complexity drift: compare against the stored class BEFORE
    # overwriting it. The finding lands in the same list as behavioural
    # drift and liveness.
    _emit_class_change(db, system, row.t_class, workload["t_class"], workload)
    row.verdict_status = (scored.get("verdict") or {}).get("status")
    row.t_class = workload["t_class"]
    row.k_median = workload["k_median"]
    row.a_max = workload["a_max"]

    row.health_state = scored["health_state"]
    row.health_score = scored["health_score"]
    row.liveness_score = scored.get("liveness_score")
    row.behaviour_score = scored.get("behaviour_score")
    row.reliability_score = scored.get("reliability_score")
    row.cost_score = scored.get("cost_score")
    row.conformance_score = scored.get("conformance_score")
    row.oversight_score = scored.get("oversight_score")
    row.review_score = scored.get("review_score")
    row.chain_integrity_pct = scored.get("chain_integrity_pct")
    row.baseline_state = scored.get("baseline_state")
    row.runs_24h = scored.get("runs_24h", 0)
    row.open_drift_events = scored.get("open_drift_events", 0)
    row.reasons = scored.get("reasons", [])
    # The cap is stored WITH the state by the same
    # computation, so the health endpoint shows the cap the state was
    # scored under (recomputing it live could disagree for a sweep).
    row.dependency_cap = scored.get("dependency_cap")
    row.recent_runs = scored.get("recent_runs")
    row.computed_at = datetime.now(timezone.utc)
    db.flush()
    return row


def capture(
    db: Session,
    system: ResourceGroup,
    *,
    period_hours: int = DEFAULT_PERIOD_HOURS,
    trigger: str = REPORT_TRIGGER_MANUAL,
    captured_by: Optional[str] = None,
) -> AISystemAssuranceReport:
    """Freeze a report. Immutable once written."""
    report = build_report(db, system, period_hours=period_hours)
    active = contracts.current_contract(db, system.tenant_id, system.id)

    row = AISystemAssuranceReport(
        id=f"asr-{ULID()}",
        tenant_id=system.tenant_id,
        ai_system_id=system.id,
        period_hours=period_hours,
        report_json=report,
        health_state=report["health"]["health_state"],
        contract_version=active.version if active else None,
        contract_hash=active.contract_hash if active else None,
        trigger=trigger,
        captured_by=captured_by,
    )

    # Attest the frozen bytes. The digest is over the canonical JSON
    # of the report as stored; the signature (deployment audit key, via the
    # proxy) is what makes recomputing the chain require a key. Best-effort:
    # an unsigned snapshot says so rather than implying otherwise.
    canonical = canonical_json(report)
    manifest = {
        "kind": standards.REPORT_SPEC,
        "spec_version": standards.REPORT_SPEC_VERSION,
        "report_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "tenant_id": system.tenant_id,
        "ai_system_id": system.id,
        "period_hours": period_hours,
        "generated_at": report["generated_at"],
        "contract_version": active.version if active else None,
        "contract_hash": active.contract_hash if active else None,
    }
    row.signature_json = sign_manifest(manifest)

    db.add(row)
    db.flush()
    log.info(
        "captured assurance report %s for %s (%s, signed=%s)",
        row.id, system.id, trigger, row.signature_json.get("signed"),
    )
    return row


def certificate_document(row: AISystemAssuranceReport) -> Dict[str, Any]:
    """A captured snapshot as a portable, verifiable certificate.

    Self-contained: the report, the digest, and (when the deployment has an
    audit key) the Ed25519 signature and public key. A recipient verifies
    with `sha256sum` + any Ed25519 library — no API call back to us.
    """
    return {
        "spec": standards.REPORT_SPEC,
        "spec_version": standards.REPORT_SPEC_VERSION,
        "certificate": {
            "snapshot_id": row.id,
            "captured_at": row.created_at.isoformat() if row.created_at else None,
            "captured_by": row.captured_by,
            "trigger": row.trigger,
            "contract_version": row.contract_version,
            "contract_hash": row.contract_hash,
        },
        "report": row.report_json,
        "attestation": row.signature_json
        or {"signed": False, "unsigned_reason": "captured before reports were signed"},
    }


__all__ = [
    "DEFAULT_PERIOD_HOURS",
    "build_report",
    "capture",
    "certificate_document",
    "compute_health",
    "upsert_health",
]
