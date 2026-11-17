# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Judged evidence — verdicts and human grades are records

Tier-B assurance checks already carry an evaluator fingerprint,
deterministic disclosed sampling and budgets. Three additions make their
verdicts evidence rather than opinion:

1. **The rubric is frozen and sealed.** Per check and period, the rubric
   the judge applies (its text, labels, output type, threshold, spec hash)
   is stored in `assurance_check_rubrics` and sealed as an
   `ai.brutor.rubric` record whose payload digest covers it — the rubric
   body never enters a record. Every verdict in the period references the
   rubric digest. Freezing is idempotent: the same rubric in the same
   period is one row and one record; a changed rubric closes the open
   period and opens a new one.
2. **Every judged verdict is an `ai.brutor.assessment` record** (verdict
   `assessment`), the value in `controls[]`, the rubric digest in
   `references[]`, chained `assesses` → the run's last action record when
   one exists. Best-effort by construction: a seal failure is logged and
   the check run continues — the verdict is still stored, just unsealed.
3. **A weekly deterministic, stratified, blind sample** of judged verdicts
   is queued for a human (`assurance_check_reviews`). The sample is seeded
   by `(check, week)` so the same inputs always draw the same sample, and
   stratified by judged result so a judge that says "met" 98 % of the time
   still has its "not met" verdicts reviewed. While a review is pending the
   judge's answer is hidden from the reviewer (`to_dict(reveal_judged=
   False)`). A grade seals an `ai.brutor.adjudication` record (disposition
   accept/reject — whether the human's independent grade agrees with the
   judge — approver human, `human_disposed: true`, authority = the review
   id) chained `adjudicates` → the assessment.

Agreement per `(check, evaluator_fingerprint)` over 30 days is computed by
`app/services/compliance/agreement.py` (observed agreement, κ / MAE, Wilson
95 % interval, `insufficient_sample` below 10) and reset by construction on
a fingerprint change — reviews are keyed by fingerprint. Below 0.80 the
check raises `judge_uncalibrated`; 14 days without a single graded review
while reviews are queued raises `calibration_stalled`.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from ulid import ULID

from app.database.models.action_record import (
    VERDICT_ADJUDICATION,
    VERDICT_ASSESSMENT,
    ActionRecord,
)
from app.database.models.assurance_check import (
    CHECK_OUTPUT_BOOLEAN,
    CHECK_OUTPUT_SCORE,
    CHECK_TIER_SEMANTIC,
    AssuranceCheck,
    AssuranceCheckResult,
)
from app.database.models.assurance_check_calibration import (
    REVIEW_RESULTS,
    REVIEW_STATUS_GRADED,
    REVIEW_STATUS_PENDING,
    AssuranceCheckReview,
    AssuranceCheckRubric,
)
from app.services.compliance import sealing

log = logging.getLogger("brutor.compliance.judged")

#: Agreement window and thresholds — owned by `compliance.agreement`.
from app.services.compliance.agreement import (  # noqa: E402
    AGREEMENT_WINDOW_DAYS,
    CALIBRATION_THRESHOLD as UNCALIBRATED_BELOW,
)
STALLED_AFTER_DAYS = 14

#: Weekly sample: a target size split across strata (judged result), with
#: a floor per stratum so a rare verdict is always reviewed.
WEEKLY_SAMPLE_TARGET = 10
MIN_PER_STRATUM = 2

RESULT_MET = "met"
RESULT_NOT_MET = "not_met"
RESULT_NA = "n/a"

_CONTROL_RESULT = {RESULT_MET: "pass", RESULT_NOT_MET: "fail", RESULT_NA: "n/a"}


class ReviewError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def week_start(day: date) -> date:
    """Monday of the ISO week containing `day`."""
    return day - timedelta(days=day.weekday())


def period_start(day: date) -> date:
    """Rubrics are frozen per calendar month (the Evidence Report period)."""
    return day.replace(day=1)


# ---------------------------------------------------------------------------
# Verdict → judged result
# ---------------------------------------------------------------------------


def judged_result(check: AssuranceCheck, value: Any) -> str:
    """`met | not_met | n/a` for one judged value.

    A value-threshold check reads "breach ⇒ not met"; a boolean check with
    no threshold reads true ⇒ met; anything else (scores without a
    threshold, classifications, extractions) is `n/a` — there is no honest
    met/not-met reading of it."""
    from app.services.assurance_checks.judge import _value_breached

    threshold = check.threshold or {}
    if threshold.get("kind") == "value":
        return RESULT_NOT_MET if _value_breached(threshold, value) else RESULT_MET
    if check.output == CHECK_OUTPUT_BOOLEAN and isinstance(value, bool):
        return RESULT_MET if value else RESULT_NOT_MET
    return RESULT_NA


def judged_score(check: AssuranceCheck, value: Any) -> Optional[float]:
    if check.output == CHECK_OUTPUT_SCORE and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def template_of(check: AssuranceCheck) -> Optional[str]:
    """The tier-B template a check implements (`evaluator.template`), when
    declared — what `judge.<template>` statements select checks by."""
    template = (check.evaluator or {}).get("template")
    return template if isinstance(template, str) and template else None


def control_id(check: AssuranceCheck) -> str:
    """`ai.brutor.judge.<template>` for a template-bound check (what the
    `judge.<template>` statement reads), else `ai.brutor.check.<id>`."""
    template = template_of(check)
    return f"ai.brutor.judge.{template}" if template else f"ai.brutor.check.{check.id}"


# ---------------------------------------------------------------------------
# 1. Rubric freeze
# ---------------------------------------------------------------------------


def rubric_body(check: AssuranceCheck) -> Dict[str, Any]:
    """Everything the judge is told to apply — the thing a verdict is
    judged AGAINST. Model choice is not rubric (it is in the fingerprint)."""
    evaluator = check.evaluator or {}
    return {
        "check_id": check.id,
        "check_version": check.version,
        "spec_hash": check.spec_hash,
        "template": template_of(check),
        "output": check.output,
        "rubric": evaluator.get("rubric") or "",
        "labels": evaluator.get("labels"),
        "threshold": check.threshold,
        "sample_rate": evaluator.get("sample_rate", 1.0),
    }


@dataclass
class RubricFreeze:
    rubric: AssuranceCheckRubric
    created: bool
    seal: Optional[sealing.SealOutcome]


def freeze_rubric(db: Session, check: AssuranceCheck, *, today: Optional[date] = None) -> RubricFreeze:
    """Idempotently freeze the check's current rubric for the current
    period. Re-seals a row that exists but was never sealed (e.g. the
    core was unreachable last time)."""
    today = today or _now().date()
    body = rubric_body(check)
    digest = sealing.json_digest(body)
    start = period_start(today)

    current = (
        db.query(AssuranceCheckRubric)
        .filter(
            AssuranceCheckRubric.check_id == check.id,
            AssuranceCheckRubric.period_end.is_(None),
        )
        .order_by(AssuranceCheckRubric.period_start.desc())
        .first()
    )
    if current is not None and current.rubric_digest == digest and current.period_start >= start:
        outcome = None
        if current.record_id is None:
            outcome = _seal_rubric(check, current)
            current.record_id = outcome.record_id
            db.flush()
        return RubricFreeze(current, False, outcome)

    if current is not None:
        if current.rubric_digest == digest:
            # Same rubric, new month: the open period ends the day before.
            start = period_start(today)
            current.period_end = start - timedelta(days=1)
        else:
            # Changed rubric: the old one covered verdicts up to yesterday
            # (or only today, when it was frozen today).
            start = today
            current.period_end = max(current.period_start, today - timedelta(days=1))
        db.flush()

    existing = (
        db.query(AssuranceCheckRubric)
        .filter(
            AssuranceCheckRubric.check_id == check.id,
            AssuranceCheckRubric.rubric_digest == digest,
            AssuranceCheckRubric.period_start == start,
        )
        .first()
    )
    if existing is not None:
        existing.period_end = None
        db.flush()
        return RubricFreeze(existing, False, None)

    row = AssuranceCheckRubric(
        id=f"rub-{ULID()}".lower(),
        tenant_id=check.tenant_id,
        check_id=check.id,
        rubric_digest=digest,
        rubric=body,
        period_start=start,
        period_end=None,
    )
    db.add(row)
    db.flush()
    outcome = _seal_rubric(check, row)
    row.record_id = outcome.record_id
    db.flush()
    return RubricFreeze(row, True, outcome)


def _seal_rubric(check: AssuranceCheck, row: AssuranceCheckRubric) -> sealing.SealOutcome:
    template = template_of(check)
    return sealing.seal(
        check.tenant_id,
        sealing.RECORD_RUBRIC,
        context={
            "check_id": check.id,
            "period_start": row.period_start.isoformat(),
            "tags": [f"judge.{template}"] if template else None,
        },
        payload=row.rubric,
        references=sealing.references(sealing.reference("ai.brutor.rubric", row.rubric_digest, "rubric")),
    )


def current_rubric(db: Session, check_id: str) -> Optional[AssuranceCheckRubric]:
    return (
        db.query(AssuranceCheckRubric)
        .filter(AssuranceCheckRubric.check_id == check_id, AssuranceCheckRubric.period_end.is_(None))
        .order_by(AssuranceCheckRubric.period_start.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# 2. Assessment per judged verdict
# ---------------------------------------------------------------------------


def _run_record(db: Session, tenant_id: str, root_task_id: Optional[str]) -> Optional[str]:
    """The run's last action record (what the assessment `assesses`)."""
    if not root_task_id:
        return None
    row = (
        db.query(ActionRecord.record_id)
        .filter(
            ActionRecord.tenant_id == tenant_id,
            ActionRecord.root_task_id == root_task_id,
            ActionRecord.verdict.notin_((VERDICT_ASSESSMENT, VERDICT_ADJUDICATION)),
        )
        .order_by(ActionRecord.timestamp.desc(), ActionRecord.seq.desc())
        .first()
    )
    return row[0] if row else None


def assessment_record(
    db: Session,
    check: AssuranceCheck,
    result: AssuranceCheckResult,
    *,
    root_task_id: Optional[str],
    ai_system_id: Optional[str],
    rubric: Optional[AssuranceCheckRubric],
) -> Dict[str, Any]:
    value = (result.value or {}).get("value")
    outcome = judged_result(check, value)
    template = template_of(check)
    control: Dict[str, Any] = {
        "id": control_id(check),
        "result": _CONTROL_RESULT[outcome],
        "blocking": False,
        "kind": "judged",
    }
    parent = _run_record(db, check.tenant_id, root_task_id)
    return sealing.build_record(
        sealing.RECORD_ASSESSMENT,
        verdict=VERDICT_ASSESSMENT,
        kind="fyi",
        ai_system_id=ai_system_id,
        context={
            "check_id": check.id,
            "evaluator_fingerprint": result.evaluator_fingerprint,
            "root_task_id": root_task_id,
            "tags": [f"judge.{template}"] if template else None,
        },
        controls=[control],
        # The judged value and result — a score or label can be free-ish
        # text, so it enters as a digest; the met/not-met reading is the
        # control result above.
        payload={"check_id": check.id, "run_id": result.run_id, "value": value, "judged_result": outcome},
        references=sealing.references(
            sealing.reference("ai.brutor.rubric", rubric.rubric_digest if rubric else None, "rubric")
        ),
        chain={"parent_record_id": parent, "relation": "assesses"} if parent else None,
    )


def seal_assessment(
    db: Session,
    check: AssuranceCheck,
    result: AssuranceCheckResult,
    *,
    root_task_id: Optional[str],
    ai_system_id: Optional[str],
    rubric: Optional[AssuranceCheckRubric] = None,
) -> sealing.SealOutcome:
    """Seal one judged verdict and stamp `assessment_record_id` (and the
    rubric digest) into the result's evidence pointers. NEVER raises —
    called from the judge loop, which must not fail over evidence."""
    try:
        if check.tier != CHECK_TIER_SEMANTIC:
            return sealing.SealOutcome(error="not_judged", detail="deterministic checks are recomputed, not judged")
        rubric = rubric or current_rubric(db, check.id)
        record = assessment_record(
            db, check, result, root_task_id=root_task_id, ai_system_id=ai_system_id, rubric=rubric
        )
        outcome = sealing.seal_many(check.tenant_id, [record])[0]
        evidence = dict(result.evidence or {})
        evidence["judged_result"] = judged_result(check, (result.value or {}).get("value"))
        if rubric is not None:
            evidence["rubric_digest"] = rubric.rubric_digest
        if outcome.sealed:
            evidence["assessment_record_id"] = outcome.record_id
        result.evidence = evidence
        flag_modified(result, "evidence")
        db.flush()
        return outcome
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        log.warning("assessment seal skipped for check %s: %s", check.id, exc)
        return sealing.SealOutcome(error="internal", detail=str(exc)[:300])


# ---------------------------------------------------------------------------
# 3. Weekly blind sample
# ---------------------------------------------------------------------------


def sample_seed(check_id: str, week: date) -> str:
    return hashlib.sha256(f"{check_id}|{week.isoformat()}".encode("utf-8")).hexdigest()


def _order_key(seed: str, sample_key: str) -> str:
    return hashlib.sha256(f"{seed}|{sample_key}".encode("utf-8")).hexdigest()


def allocate(strata: Dict[str, int], target: int = WEEKLY_SAMPLE_TARGET,
             floor: int = MIN_PER_STRATUM) -> Dict[str, int]:
    """Per-stratum quotas: proportional to size, never below `floor` (or
    the stratum's size when smaller)."""
    total = sum(strata.values())
    if total == 0:
        return {k: 0 for k in strata}
    quotas = {}
    for name, size in strata.items():
        proportional = math.ceil(target * size / total)
        quotas[name] = min(size, max(floor, proportional))
    return quotas


def draw_sample(
    items: Sequence[Tuple[str, str]], seed: str, *, target: int = WEEKLY_SAMPLE_TARGET,
    floor: int = MIN_PER_STRATUM,
) -> List[Tuple[str, str]]:
    """Deterministic stratified sample of `(sample_key, stratum)` pairs.
    Same items + same seed ⇒ same sample, regardless of input order."""
    by_stratum: Dict[str, List[str]] = {}
    for key, stratum in items:
        by_stratum.setdefault(stratum, []).append(key)
    quotas = allocate({k: len(v) for k, v in by_stratum.items()}, target, floor)
    picked: List[Tuple[str, str]] = []
    for stratum in sorted(by_stratum):
        ordered = sorted(set(by_stratum[stratum]), key=lambda k: _order_key(seed, k))
        picked.extend((k, stratum) for k in ordered[: quotas[stratum]])
    return sorted(picked, key=lambda p: _order_key(seed, p[0]))


def sample_week(db: Session, check: AssuranceCheck, week: date) -> List[AssuranceCheckReview]:
    """Queue the week's blind sample for one judged check. Idempotent:
    re-running the same week adds nothing (unique per check, fingerprint,
    sample key). Returns the rows created now."""
    week = week_start(week)
    start = datetime.combine(week, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    results = (
        db.query(AssuranceCheckResult)
        .filter(
            AssuranceCheckResult.check_id == check.id,
            AssuranceCheckResult.backtest.is_(False),
            AssuranceCheckResult.run_id.isnot(None),
            AssuranceCheckResult.evaluated_at >= start,
            AssuranceCheckResult.evaluated_at < end,
        )
        .all()
    )
    if not results:
        return []
    by_key: Dict[str, AssuranceCheckResult] = {}
    for r in results:
        # One verdict per (run, fingerprint); the newest wins on a re-judge.
        key = r.run_id
        prior = by_key.get(key)
        if prior is None or (r.evaluated_at or start) > (prior.evaluated_at or start):
            by_key[key] = r
    items = [(k, judged_result(check, (r.value or {}).get("value"))) for k, r in by_key.items()]
    picked = draw_sample(items, sample_seed(check.id, week))

    existing = {
        (fp, key)
        for fp, key in db.query(AssuranceCheckReview.evaluator_fingerprint, AssuranceCheckReview.sample_key)
        .filter(AssuranceCheckReview.check_id == check.id)
        .all()
    }
    created: List[AssuranceCheckReview] = []
    for key, stratum in picked:
        r = by_key[key]
        if (r.evaluator_fingerprint, key) in existing:
            continue
        row = AssuranceCheckReview(
            id=f"rev-{ULID()}".lower(),
            tenant_id=check.tenant_id,
            check_id=check.id,
            evaluator_fingerprint=r.evaluator_fingerprint,
            week=week,
            sample_key=key,
            assessment_record_id=(r.evidence or {}).get("assessment_record_id"),
            judged_result=stratum,
            judged_score=judged_score(check, (r.value or {}).get("value")),
            status=REVIEW_STATUS_PENDING,
            blind=True,
        )
        db.add(row)
        created.append(row)
    db.flush()
    return created


# ---------------------------------------------------------------------------
# Grading → adjudication
# ---------------------------------------------------------------------------


@dataclass
class Grade:
    review: AssuranceCheckReview
    seal: sealing.SealOutcome
    agrees: bool


def grade_review(
    db: Session,
    check: AssuranceCheck,
    review: AssuranceCheckReview,
    *,
    human_result: str,
    human_score: Optional[float] = None,
    reviewer: Optional[str] = None,
) -> Grade:
    if review.status != REVIEW_STATUS_PENDING:
        raise ReviewError(f"Review is already {review.status}", 409)
    if human_result not in REVIEW_RESULTS:
        raise ReviewError(f"human_result must be one of {sorted(REVIEW_RESULTS)}")
    if human_score is not None and not (0.0 <= float(human_score) <= 1.0):
        raise ReviewError("human_score must be within 0..1")
    agrees = human_result == review.judged_result
    review.human_result = human_result
    review.human_score = Decimal(str(human_score)) if human_score is not None else None
    review.status = REVIEW_STATUS_GRADED
    review.reviewer_id = reviewer  # CP-only; never enters the record
    review.reviewed_at = _now()
    db.flush()

    template = template_of(check)
    parent = review.assessment_record_id
    outcome = sealing.seal(
        review.tenant_id,
        sealing.RECORD_ADJUDICATION,
        verdict=VERDICT_ADJUDICATION,
        kind="decide",
        context={
            "check_id": check.id,
            "evaluator_fingerprint": review.evaluator_fingerprint,
            "tags": [f"judge.{template}"] if template else None,
        },
        disposition={
            "decision": "accept" if agrees else "reject",
            "approver": "human",
            "human_disposed": True,
            "authority": review.id,
        },
        controls=[{
            "id": control_id(check),
            "result": _CONTROL_RESULT[human_result],
            "blocking": False,
            "kind": "human_grade",
        }],
        payload={
            "review_id": review.id,
            "sample_key": review.sample_key,
            "week": review.week.isoformat() if review.week else None,
            "judged_result": review.judged_result,
            "human_result": human_result,
            "human_score": human_score,
            "blind": bool(review.blind),
        },
        chain=(
            {"parent_record_id": parent, "relation": "adjudicates"}
            if sealing.reference("x", parent) is not None
            else None
        ),
    )
    review.adjudication_record_id = outcome.record_id
    db.flush()
    return Grade(review=review, seal=outcome, agrees=agrees)


# ---------------------------------------------------------------------------
# Agreement + calibration findings
# ---------------------------------------------------------------------------


def current_fingerprint(db: Session, check: AssuranceCheck) -> Optional[str]:
    """The fingerprint of the check's newest judged verdict (a model or
    rubric change moves it; agreement resets with it)."""
    row = (
        db.query(AssuranceCheckResult.evaluator_fingerprint)
        .filter(
            AssuranceCheckResult.check_id == check.id,
            AssuranceCheckResult.backtest.is_(False),
            AssuranceCheckResult.run_id.isnot(None),
        )
        .order_by(AssuranceCheckResult.evaluated_at.desc())
        .first()
    )
    return row[0] if row else None


def graded_pairs(
    db: Session, check_id: str, fingerprint: str, *, now: Optional[datetime] = None,
    window_days: int = AGREEMENT_WINDOW_DAYS,
) -> List[AssuranceCheckReview]:
    since = (now or _now()) - timedelta(days=window_days)
    return (
        db.query(AssuranceCheckReview)
        .filter(
            AssuranceCheckReview.check_id == check_id,
            AssuranceCheckReview.evaluator_fingerprint == fingerprint,
            AssuranceCheckReview.status == REVIEW_STATUS_GRADED,
            AssuranceCheckReview.reviewed_at >= since,
        )
        .all()
    )


def agreement_for(
    db: Session, check: AssuranceCheck, *, fingerprint: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Agreement for `(check, fingerprint)` over 30 days — the statistics
    are `compliance.agreement.summarize` (the one implementation). Defaults
    to the check's current fingerprint: a new fingerprint starts from zero
    reviews."""
    from app.services.compliance import agreement

    fingerprint = fingerprint or current_fingerprint(db, check)
    reviews = graded_pairs(db, check.id, fingerprint, now=now) if fingerprint else []
    out = agreement.summarize(
        categorical=[(r.judged_result, r.human_result) for r in reviews],
        numeric=[
            (float(r.judged_score) if r.judged_score is not None else None,
             float(r.human_score) if r.human_score is not None else None)
            for r in reviews
        ],
        blind=all(bool(r.blind) for r in reviews) if reviews else True,
    )
    out.update({
        "check_id": check.id,
        "evaluator_fingerprint": fingerprint,
        "window_days": AGREEMENT_WINDOW_DAYS,
        "label": agreement.render(out),
    })
    return out


def evaluate_calibration(db: Session, check: AssuranceCheck, *, now: Optional[datetime] = None,
                         inbox=None) -> Dict[str, Any]:
    """Raise `judge_uncalibrated` / `calibration_stalled` for one check.
    Flushes; the caller commits. Returns what it found."""
    from app.services.assurance_inbox import AssuranceInboxService

    now = now or _now()
    inbox = inbox or AssuranceInboxService()
    found: Dict[str, Any] = {"uncalibrated": False, "stalled": False}

    stats = agreement_for(db, check, now=now)
    found["agreement"] = stats
    observed = stats.get("observed_agreement")
    if stats.get("below_threshold"):
        found["uncalibrated"] = True
        inbox.raise_item(
            db,
            tenant_id=check.tenant_id,
            category="judge_uncalibrated",
            severity="warning",
            fingerprint=f"judge_uncalibrated:{check.id}:{stats.get('evaluator_fingerprint')}",
            title=(
                f"Judge for '{check.name}' agrees with human reviewers {observed:.2f} of the time "
                f"(below {UNCALIBRATED_BELOW:.2f}, n = {stats.get('n')})"
            ),
            evidence={
                "check_id": check.id,
                "evaluator_fingerprint": stats.get("evaluator_fingerprint"),
                "observed_agreement": observed,
                "n": stats.get("n"),
            },
        )

    has_reviews = (
        db.query(AssuranceCheckReview.id).filter(AssuranceCheckReview.check_id == check.id).first() is not None
    )
    if has_reviews:
        last_graded = (
            db.query(AssuranceCheckReview.reviewed_at)
            .filter(
                AssuranceCheckReview.check_id == check.id,
                AssuranceCheckReview.status == REVIEW_STATUS_GRADED,
            )
            .order_by(AssuranceCheckReview.reviewed_at.desc())
            .first()
        )
        oldest_pending = (
            db.query(AssuranceCheckReview.created_at)
            .filter(
                AssuranceCheckReview.check_id == check.id,
                AssuranceCheckReview.status == REVIEW_STATUS_PENDING,
            )
            .order_by(AssuranceCheckReview.created_at.asc())
            .first()
        )
        cutoff = now - timedelta(days=STALLED_AFTER_DAYS)
        last = _aware(last_graded[0]) if last_graded and last_graded[0] else None
        pending_since = _aware(oldest_pending[0]) if oldest_pending and oldest_pending[0] else None
        if pending_since is not None and pending_since <= cutoff and (last is None or last <= cutoff):
            found["stalled"] = True
            inbox.raise_item(
                db,
                tenant_id=check.tenant_id,
                category="calibration_stalled",
                severity="warning",
                fingerprint=f"calibration_stalled:{check.id}",
                title=(
                    f"No blind reviews graded for '{check.name}' in {STALLED_AFTER_DAYS} days — "
                    "its judged rows carry no current human agreement"
                ),
                evidence={
                    "check_id": check.id,
                    "last_graded_at": last.isoformat() if last else None,
                    "oldest_pending_at": pending_since.isoformat(),
                },
            )
    db.flush()
    return found


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def queue_counts(db: Session, tenant_id: str, check_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, int]]:
    """`{check_id: {pending, graded, skipped}}` for the oversight page."""
    from sqlalchemy import func

    q = db.query(
        AssuranceCheckReview.check_id, AssuranceCheckReview.status, func.count(AssuranceCheckReview.id)
    ).filter(AssuranceCheckReview.tenant_id == tenant_id)
    if check_ids is not None:
        q = q.filter(AssuranceCheckReview.check_id.in_(list(check_ids)))
    out: Dict[str, Dict[str, int]] = {}
    for check_id, status, count in q.group_by(AssuranceCheckReview.check_id, AssuranceCheckReview.status).all():
        out.setdefault(check_id, {"pending": 0, "graded": 0, "skipped": 0})[status] = int(count)
    return out


# ---------------------------------------------------------------------------
# Scheduler entry (weekly)
# ---------------------------------------------------------------------------


def run_judged_cycle(db: Session, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """For every enabled semantic check: freeze the rubric, queue last
    week's sample, evaluate calibration. Each check commits on its own; one
    failing check never stops the rest."""
    now = now or _now()
    stats = {"rubrics_frozen": 0, "reviews_queued": 0, "uncalibrated": 0, "stalled": 0}
    checks = (
        db.query(AssuranceCheck)
        .filter(AssuranceCheck.enabled.is_(True), AssuranceCheck.tier == CHECK_TIER_SEMANTIC)
        .all()
    )
    last_week = week_start(now.date()) - timedelta(days=7)
    for check in checks:
        try:
            if freeze_rubric(db, check, today=now.date()).created:
                stats["rubrics_frozen"] += 1
            stats["reviews_queued"] += len(sample_week(db, check, last_week))
            found = evaluate_calibration(db, check, now=now)
            stats["uncalibrated"] += int(found["uncalibrated"])
            stats["stalled"] += int(found["stalled"])
            db.commit()
        except Exception:  # noqa: BLE001 — one check must not stop the rest
            db.rollback()
            log.exception("judged evidence cycle failed for check %s", check.id)
    return stats


__all__ = [
    "AGREEMENT_WINDOW_DAYS",
    "Grade",
    "MIN_PER_STRATUM",
    "ReviewError",
    "RubricFreeze",
    "STALLED_AFTER_DAYS",
    "UNCALIBRATED_BELOW",
    "WEEKLY_SAMPLE_TARGET",
    "agreement_for",
    "allocate",
    "current_fingerprint",
    "current_rubric",
    "draw_sample",
    "evaluate_calibration",
    "freeze_rubric",
    "grade_review",
    "judged_result",
    "queue_counts",
    "run_judged_cycle",
    "rubric_body",
    "sample_seed",
    "sample_week",
    "seal_assessment",
    "template_of",
    "week_start",
]
