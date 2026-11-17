# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Judged-check calibration — frozen rubrics and blind human reviews

A judged assurance check is only as good as its judge. Two tables make the
judge's agreement with humans measurable:

* `assurance_check_rubrics` — the rubric a check was judged against,
  frozen for a period (`period_start` .. `period_end`, open-ended while
  current) and sealed as an `ai.brutor.rubric` record. Unique per
  (check, rubric digest, period start).
* `assurance_check_reviews` — a weekly sample of judged verdicts put in
  front of a human, blind by default (the reviewer does not see the
  judge's result). The judged verdict is sealed as `ai.brutor.assessment`
  (`assessment_record_id`); the human grade as `ai.brutor.adjudication`
  (`adjudication_record_id`, chain `adjudicates` → the assessment).
  `reviewer_id` is CP-only — it never enters a record.

Both are written by the control plane.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_columns import check_vocab, created_at_column, iso, number

REVIEW_RESULTS = frozenset({"met", "not_met", "n/a"})

REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_GRADED = "graded"
REVIEW_STATUS_SKIPPED = "skipped"
REVIEW_STATUSES = frozenset({REVIEW_STATUS_PENDING, REVIEW_STATUS_GRADED, REVIEW_STATUS_SKIPPED})


class AssuranceCheckRubric(Base):
    """A judged check's rubric, frozen for a period (CP-written)."""

    __tablename__ = "assurance_check_rubrics"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'rub-'")
    tenant_id = Column(String(255), nullable=False)
    check_id = Column(
        String(64), ForeignKey("assurance_checks.id", ondelete="CASCADE"), nullable=False
    )
    rubric_digest = Column(String(64), nullable=False, comment="hex SHA-256 of JCS(rubric)")
    rubric = Column(JSONB, nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=True, comment="NULL while current")
    record_id = Column(String(64), nullable=True, comment="ai.brutor.rubric record; NULL = not sealed")
    created_at = created_at_column()

    __table_args__ = (
        Index(
            "uq_assurance_check_rubrics_period",
            "check_id",
            "rubric_digest",
            "period_start",
            unique=True,
        ),
        Index("ix_assurance_check_rubrics_tenant", "tenant_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "check_id": self.check_id,
            "rubric_digest": self.rubric_digest,
            "rubric": self.rubric,
            "period_start": iso(self.period_start),
            "period_end": iso(self.period_end),
            "record_id": self.record_id,
            "created_at": iso(self.created_at),
        }


class AssuranceCheckReview(Base):
    """One sampled judged verdict and its (blind) human grade (CP-written)."""

    __tablename__ = "assurance_check_reviews"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'rev-'")
    tenant_id = Column(String(255), nullable=False)
    check_id = Column(
        String(64), ForeignKey("assurance_checks.id", ondelete="CASCADE"), nullable=False
    )
    evaluator_fingerprint = Column(String(64), nullable=False)
    week = Column(Date, nullable=False, comment="Monday of the sampling week")
    sample_key = Column(String(255), nullable=False, comment="run / turn id the verdict is about")
    assessment_record_id = Column(String(64), nullable=True)
    judged_result = Column(String(16), nullable=False, comment="met | not_met | n/a")
    judged_score = Column(Numeric(10, 4), nullable=True)
    human_result = Column(String(16), nullable=True)
    human_score = Column(Numeric(10, 4), nullable=True)
    status = Column(
        String(16), nullable=False, default=REVIEW_STATUS_PENDING, server_default="pending"
    )
    blind = Column(Boolean, nullable=False, default=True, server_default="true")
    reviewer_id = Column(String(255), nullable=True, comment="CP-only; never enters a record")
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    adjudication_record_id = Column(String(64), nullable=True)
    created_at = created_at_column()

    __table_args__ = (
        Index(
            "uq_assurance_check_reviews_sample",
            "check_id",
            "evaluator_fingerprint",
            "sample_key",
            unique=True,
        ),
        Index("ix_assurance_check_reviews_queue", "tenant_id", "status", "week"),
    )

    @validates("judged_result")
    def _validate_judged(self, _key, value):
        return check_vocab("judged_result", value, REVIEW_RESULTS)

    @validates("human_result")
    def _validate_human(self, _key, value):
        return check_vocab("human_result", value, REVIEW_RESULTS, nullable=True)

    @validates("status")
    def _validate_status(self, _key, value):
        return check_vocab("status", value, REVIEW_STATUSES)

    def to_dict(self, *, reveal_judged: bool = True) -> Dict[str, Any]:
        """`reveal_judged=False` is the blind reviewer's view: the judge's
        verdict is withheld until the human has graded."""
        hide = not reveal_judged and bool(self.blind) and self.status == REVIEW_STATUS_PENDING
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "check_id": self.check_id,
            "evaluator_fingerprint": self.evaluator_fingerprint,
            "week": iso(self.week),
            "sample_key": self.sample_key,
            "assessment_record_id": self.assessment_record_id,
            "judged_result": None if hide else self.judged_result,
            "judged_score": None if hide else number(self.judged_score),
            "human_result": self.human_result,
            "human_score": number(self.human_score),
            "status": self.status,
            "blind": bool(self.blind),
            "reviewer_id": self.reviewer_id,
            "reviewed_at": iso(self.reviewed_at),
            "adjudication_record_id": self.adjudication_record_id,
            "created_at": iso(self.created_at),
        }


__all__ = [
    "AssuranceCheckRubric",
    "AssuranceCheckReview",
    "REVIEW_RESULTS",
    "REVIEW_STATUSES",
    "REVIEW_STATUS_PENDING",
    "REVIEW_STATUS_GRADED",
    "REVIEW_STATUS_SKIPPED",
]
