# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Judged evidence — rubrics, assessments, blind reviews, agreement

* Rubric freeze is idempotent per (check, period); a changed rubric closes
  the open period; the rubric body enters only as a payload digest.
* Every judged verdict seals an `assessment` record: value in controls[],
  rubric digest in references[], chained `assesses` → the run's record;
  sealing is best-effort (a down core never fails the check run).
* The weekly sample is deterministic (same (check, week) ⇒ same sample),
  stratified by judged result, and blind: the judged result is hidden
  while a review is pending.
* A grade seals an `adjudication` record (human / human_disposed=true,
  authority = review id, chain `adjudicates` → the assessment).
* Agreement is per (check, evaluator fingerprint) — a fingerprint change
  resets it — and feeds `judge_uncalibrated` (< 0.80, sufficient sample)
  and `calibration_stalled` (14 days without a graded review).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from ulid import ULID

from app.api.v1.endpoints.admin.assurance_check_reviews import create_assurance_check_reviews_router
from app.core.logging_system import BrutorLogger
from app.database.models.action_record import ActionRecord
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_check import AssuranceCheck, AssuranceCheckResult, compute_spec_hash
from app.database.models.assurance_check_calibration import AssuranceCheckReview, AssuranceCheckRubric
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import judged
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness

WEEK = date(2026, 8, 31)  # a Monday
FP = "f" * 64


def seed(session):
    session.add(ResourceGroup(
        id="sys-a", tenant_id="t1", name="sys-a", description="", hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
    ))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_assurance_check_reviews_router(BrutorLogger("judged-tests"))])
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def core(monkeypatch):
    return fakes.install(monkeypatch)


@pytest.fixture
def db(harness):
    s = harness.Factory()
    yield s
    s.rollback()
    s.close()


def make_check(s, *, rubric="Is the notice clear?", template="notice_clear", tier="semantic",
               threshold=None, output="boolean", tenant="t1"):
    evaluator = {"rubric": rubric, "model_ref": "judge-model", "sample_rate": 1.0}
    if template:
        evaluator["template"] = template
    spec = {"scope": {}, "tier": tier, "evaluator": evaluator, "output": output,
            "trigger": "batch", "threshold": threshold}
    row = AssuranceCheck(
        id=f"chk-{ULID()}", tenant_id=tenant, name=f"check-{uuid.uuid4().hex[:6]}", version=1,
        spec_hash=compute_spec_hash(spec), scope={}, tier=tier, evaluator=evaluator, output=output,
        trigger="batch", threshold=threshold, route_to_inbox=True, enabled=True,
    )
    s.add(row)
    s.flush()
    return row


def make_result(s, check, *, value, at, fp=FP, run_id=None):
    r = AssuranceCheckResult(
        id=f"ckr-{ULID()}", tenant_id=check.tenant_id, check_id=check.id, check_version=1,
        run_id=run_id or f"run-{uuid.uuid4().hex[:10]}", value={"value": value},
        evidence={"run_id": run_id}, evaluator_fingerprint=fp, backtest=False, evaluated_at=at,
    )
    s.add(r)
    s.flush()
    return r


class TestRubricFreeze:
    def test_idempotent_per_period_and_payload_only(self, db, core):
        check = make_check(db)
        first = judged.freeze_rubric(db, check, today=date(2026, 9, 10))
        assert first.created and first.rubric.period_start == date(2026, 9, 1)
        assert first.rubric.record_id == first.seal.record_id
        again = judged.freeze_rubric(db, check, today=date(2026, 9, 20))
        assert not again.created and again.rubric.id == first.rubric.id
        (rec,) = core.records("ai.brutor.rubric")
        assert rec["payload"]["rubric"] == "Is the notice clear?"
        assert rec["context"]["check_id"] == check.id and rec["context"]["tags"] == ["judge.notice_clear"]
        assert rec["references"][0]["digest"] == first.rubric.rubric_digest
        assert "Is the notice clear?" not in str({k: v for k, v in rec.items() if k != "payload"})

    def test_new_month_and_changed_rubric_close_the_open_period(self, db):
        check = make_check(db)
        a = judged.freeze_rubric(db, check, today=date(2026, 9, 10)).rubric
        b = judged.freeze_rubric(db, check, today=date(2026, 10, 3)).rubric
        assert b.id != a.id and a.period_end == date(2026, 9, 30) and b.period_start == date(2026, 10, 1)
        check.evaluator = {**check.evaluator, "rubric": "Is the notice clear AND first?"}
        c = judged.freeze_rubric(db, check, today=date(2026, 10, 7)).rubric
        assert c.rubric_digest != b.rubric_digest
        assert b.period_end == date(2026, 10, 6) and c.period_start == date(2026, 10, 7) and c.period_end is None
        open_rows = db.query(AssuranceCheckRubric).filter(
            AssuranceCheckRubric.check_id == check.id, AssuranceCheckRubric.period_end.is_(None)
        ).all()
        assert [r.id for r in open_rows] == [c.id]

    def test_unsealed_rubric_is_resealed_later(self, db, core):
        check = make_check(db)
        core.mode = fakes.MODE_UNREACHABLE
        first = judged.freeze_rubric(db, check, today=date(2026, 9, 10))
        assert first.rubric.record_id is None
        core.mode = fakes.MODE_OK
        again = judged.freeze_rubric(db, check, today=date(2026, 9, 11))
        assert again.rubric.id == first.rubric.id and again.rubric.record_id


class TestAssessment:
    def _run_record(self, s, root):
        s.add(ActionRecord(
            record_id="ab" * 32, tenant_id="t1", log_id="log", seq=int(uuid.uuid4().int % 10**9),
            ai_system_id="sys-a", root_task_id=root, verdict="executed", kind="decide", surface="llm",
            timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc), record={}, statement=b"s", kid="k" * 64,
        ))
        s.flush()

    def test_sealed_with_control_rubric_and_chain(self, db, core):
        check = make_check(db)
        rubric = judged.freeze_rubric(db, check, today=date(2026, 9, 10)).rubric
        self._run_record(db, "task-1")
        result = make_result(db, check, value=False, at=datetime(2026, 9, 2, tzinfo=timezone.utc))
        out = judged.seal_assessment(db, check, result, root_task_id="task-1", ai_system_id="sys-a", rubric=rubric)
        assert out.sealed
        (rec,) = core.records("ai.brutor.assessment")
        assert rec["verdict"] == "assessment" and rec["ai_system_id"] == "sys-a"
        assert rec["controls"] == [{"id": "ai.brutor.judge.notice_clear", "result": "fail",
                                    "blocking": False, "kind": "judged"}]
        assert rec["chain"] == {"parent_record_id": "ab" * 32, "relation": "assesses"}
        assert rec["references"][0] == {"type": "ai.brutor.rubric",
                                        "digest": rubric.rubric_digest, "purpose": "rubric"}
        assert rec["context"]["evaluator_fingerprint"] == FP
        assert result.evidence["assessment_record_id"] == out.record_id
        assert result.evidence["judged_result"] == "not_met"

    def test_no_run_record_means_no_chain(self, db, core):
        check = make_check(db, template=None)
        result = make_result(db, check, value=True, at=datetime(2026, 9, 2, tzinfo=timezone.utc))
        judged.seal_assessment(db, check, result, root_task_id="task-none", ai_system_id="sys-a")
        (rec,) = core.records("ai.brutor.assessment")
        assert "chain" not in rec and rec["controls"][0]["id"] == f"ai.brutor.check.{check.id}"

    @pytest.mark.parametrize("mode", [fakes.MODE_DISABLED, fakes.MODE_UNREACHABLE])
    def test_best_effort(self, db, core, mode):
        core.mode = mode
        check = make_check(db)
        result = make_result(db, check, value=True, at=datetime(2026, 9, 2, tzinfo=timezone.utc))
        out = judged.seal_assessment(db, check, result, root_task_id=None, ai_system_id="sys-a")
        assert not out.sealed and out.error == mode
        assert "assessment_record_id" not in result.evidence
        assert result.evidence["judged_result"] == "met"

    def test_judge_executor_seals_every_verdict(self, db, core, monkeypatch):
        from app.services.assurance_checks.judge import JudgeExecutor

        check = make_check(db, threshold={"kind": "value", "op": "eq", "value": False, "severity": "warning"})
        now = datetime.now(timezone.utc)
        for i in range(2):
            db.add(AISystemRun(
                id=f"run-{ULID()}", tenant_id="t1", root_task_id=f"task-j{i}-{uuid.uuid4().hex[:6]}",
                ai_system_id="sys-a", started_at=now - timedelta(minutes=10), ended_at=now - timedelta(minutes=5),
                terminal_state="completed", finalized_by="explicit", total_cost_usd=0, is_replay=False,
            ))
        db.commit()
        ex = JudgeExecutor(BrutorLogger("judge"))
        monkeypatch.setattr(
            ex, "run_transcript",
            lambda *a, **k: ("MODEL REQUEST: hi\nMODEL RESPONSE: hello", {"hops": 1}),
        )
        monkeypatch.setattr(ex, "judge_call", lambda *a, **k: ('{"value": true}', 10))
        stats = ex.run_check(db, check)
        assert stats["judged"] == 2
        assessments = core.records("ai.brutor.assessment")
        assert len(assessments) == 2 and all(r["controls"][0]["result"] == "pass" for r in assessments)
        assert len(core.records("ai.brutor.rubric")) == 1
        results = db.query(AssuranceCheckResult).filter(AssuranceCheckResult.check_id == check.id).all()
        assert all(r.evidence.get("assessment_record_id") for r in results)


class TestSampling:
    def test_allocation_and_determinism(self):
        items = [(f"run-{i}", "met") for i in range(40)] + [(f"run-n{i}", "not_met") for i in range(3)]
        seed = judged.sample_seed("chk-1", WEEK)
        a = judged.draw_sample(items, seed)
        b = judged.draw_sample(list(reversed(items)), seed)
        assert a == b, "same seed ⇒ same sample, whatever the input order"
        strata = {s for _, s in a}
        assert strata == {"met", "not_met"}
        assert sum(1 for _, s in a if s == "not_met") == 2  # floor per stratum
        other = judged.draw_sample(items, judged.sample_seed("chk-1", WEEK + timedelta(days=7)))
        assert other != a
        assert judged.allocate({"met": 1, "not_met": 0}) == {"met": 1, "not_met": 0}

    def test_sample_week_is_idempotent_and_blind(self, harness, db):
        check = make_check(db)
        for i in range(12):
            make_result(db, check, value=(i % 4 != 0), at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
        make_result(db, check, value=True, at=datetime(2026, 9, 9, tzinfo=timezone.utc))  # next week
        db.commit()
        created = judged.sample_week(db, check, WEEK + timedelta(days=3))
        db.commit()
        assert created and all(r.week == WEEK for r in created)
        assert {r.judged_result for r in created} == {"met", "not_met"}
        assert judged.sample_week(db, check, WEEK) == []
        pending = created[0].to_dict(reveal_judged=False)
        assert pending["judged_result"] is None and pending["judged_score"] is None and pending["blind"]

        body = harness.client.get(f"/v1/admin/assurance-checks/{check.id}/reviews",
                                  params={"status": "pending"}, headers=harness.headers()).json()
        assert body["total"] == len(created)
        assert all(i["judged_result"] is None for i in body["items"])
        assert body["counts"]["pending"] == len(created)


class TestGradingAndAgreement:
    def _queue(self, db, check, n, fp=FP, judged_result="met", week=WEEK):
        rows = []
        for i in range(n):
            r = AssuranceCheckReview(
                id=f"rev-{ULID()}".lower(), tenant_id="t1", check_id=check.id, evaluator_fingerprint=fp,
                week=week, sample_key=f"run-{uuid.uuid4().hex[:8]}", assessment_record_id="cd" * 32,
                judged_result=judged_result, status="pending", blind=True,
            )
            db.add(r)
            rows.append(r)
        db.commit()
        return rows

    def test_grade_seals_adjudication_and_reveals(self, harness, db, core):
        check = make_check(db)
        make_result(db, check, value=True, at=datetime.now(timezone.utc))
        (review,) = self._queue(db, check, 1)
        r = harness.client.post(f"/v1/admin/assurance-checks/{check.id}/reviews/{review.id}",
                                json={"human_result": "not_met"}, headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agrees_with_judge"] is False
        assert body["review"]["judged_result"] == "met" and body["review"]["status"] == "graded"
        assert body["seal"]["sealed"] and body["review"]["adjudication_record_id"] == body["seal"]["record_id"]
        (rec,) = core.records("ai.brutor.adjudication")
        assert rec["verdict"] == "adjudication"
        assert rec["disposition"] == {"decision": "reject", "approver": "human", "human_disposed": True,
                                      "authority": review.id}
        assert rec["chain"] == {"parent_record_id": "cd" * 32, "relation": "adjudicates"}
        assert rec["controls"][0]["result"] == "fail"
        assert "reviewer" not in str(rec) and "admin_t1" not in str(rec)
        # Grading twice is refused; other tenants cannot see the check.
        again = harness.client.post(f"/v1/admin/assurance-checks/{check.id}/reviews/{review.id}",
                                    json={"human_result": "met"}, headers=harness.headers())
        assert again.status_code == 409
        assert harness.client.get(f"/v1/admin/assurance-checks/{check.id}/reviews",
                                  headers=harness.headers("t2")).status_code == 404

    def test_unsealed_grade_is_kept(self, db, core):
        check = make_check(db)
        (review,) = self._queue(db, check, 1)
        core.mode = fakes.MODE_DISABLED
        grade = judged.grade_review(db, check, review, human_result="met", reviewer="x")
        assert grade.agrees and not grade.seal.sealed and review.status == "graded"
        assert review.adjudication_record_id is None

    def test_agreement_threshold_raises_uncalibrated_and_resets_on_new_fingerprint(self, db):
        check = make_check(db)
        make_result(db, check, value=True, at=datetime.now(timezone.utc))
        rows = self._queue(db, check, 12)
        for i, r in enumerate(rows):
            judged.grade_review(db, check, r, human_result="met" if i < 8 else "not_met", reviewer="x")
        db.commit()
        stats = judged.agreement_for(db, check)
        assert stats["n"] == 12 and stats["observed_agreement"] == round(8 / 12, 3)
        assert stats["insufficient_sample"] is False and stats["below_threshold"] is True
        assert stats["ci95"][0] < stats["observed_agreement"] < stats["ci95"][1]
        found = judged.evaluate_calibration(db, check)
        db.commit()
        assert found["uncalibrated"] is True
        item = db.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.fingerprint == f"judge_uncalibrated:{check.id}:{FP}").one()
        assert item.category == "judge_uncalibrated"

        # A new evaluator fingerprint starts from zero reviews.
        make_result(db, check, value=True, at=datetime.now(timezone.utc) + timedelta(seconds=5), fp="e" * 64)
        db.commit()
        fresh = judged.agreement_for(db, check)
        assert fresh["evaluator_fingerprint"] == "e" * 64 and fresh["n"] == 0 and fresh["insufficient_sample"]
        assert judged.evaluate_calibration(db, check)["uncalibrated"] is False

    def test_small_sample_never_calls_a_judge_uncalibrated(self, db):
        check = make_check(db)
        make_result(db, check, value=True, at=datetime.now(timezone.utc))
        for r in self._queue(db, check, 5):
            judged.grade_review(db, check, r, human_result="not_met", reviewer="x")
        db.commit()
        stats = judged.agreement_for(db, check)
        assert stats["observed_agreement"] == 0.0 and stats["insufficient_sample"] is True
        assert judged.evaluate_calibration(db, check)["uncalibrated"] is False

    def test_calibration_stalled_after_14_days_without_grades(self, db):
        check = make_check(db)
        (r,) = self._queue(db, check, 1)
        r.created_at = datetime.now(timezone.utc) - timedelta(days=15)
        db.commit()
        assert judged.evaluate_calibration(db, check)["stalled"] is True
        db.commit()
        assert db.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.fingerprint == f"calibration_stalled:{check.id}").count() == 1
        # A recent grade clears the condition for the next evaluation.
        (fresh,) = self._queue(db, check, 1)
        judged.grade_review(db, check, fresh, human_result="met", reviewer="x")
        db.commit()
        assert judged.evaluate_calibration(db, check)["stalled"] is False

    def test_run_judged_cycle(self, db):
        check = make_check(db)
        last_monday = judged.week_start(datetime.now(timezone.utc).date()) - timedelta(days=7)
        for i in range(4):
            make_result(db, check, value=bool(i % 2),
                        at=datetime.combine(last_monday, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=i))
        db.commit()
        stats = judged.run_judged_cycle(db)
        assert stats["reviews_queued"] >= 4 and stats["rubrics_frozen"] >= 1
        again = judged.run_judged_cycle(db)
        assert again["reviews_queued"] == 0 and again["rubrics_frozen"] == 0


def test_hex_helper_sanity():
    assert len(hashlib.sha256(b"x").hexdigest()) == 64
