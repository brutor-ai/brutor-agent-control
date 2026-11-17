# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Staged incident deadlines (DORA, NIS2) — each stage on its own anchor

DORA (Delegated Regulation (EU) 2025/301, Art 5(1)): initial notification
within 4 h of CLASSIFICATION as major and no later than 24 h after
AWARENESS; intermediate report 72 h after the SUBMISSION of the initial
notification; final report one calendar month after the intermediate
report. NIS2 (Directive (EU) 2022/2555, Art 23(4)): early warning 24 h and
notification 72 h from awareness; final report one month after the
SUBMISSION of the notification.

The register used to measure every stage from awareness, so a bank that
classified at 10 h and notified 2 h later was shown "reported late". These
tests pin the anchors, the calendar-month arithmetic, the closed-but-
unsubmitted state, the submission endpoint's refusals, the resolver
semantics, and that the alternatives regimes (EU AI Act, GDPR, HIPAA) did
not move.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.compliance_incidents import create_compliance_incidents_router
from app.core.logging_system import BrutorLogger
from app.database.models.compliance_incident import ComplianceIncident
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import incidents as svc
from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import DeadlineStage, add_months
from app.services.compliance.resolvers.documents import _staged_outcome, incident_deadline, staged_rows
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness

H = timedelta(hours=1)
AWARE = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
DORA = CATALOGS["dora"]
NIS2 = CATALOGS["nis2"]
BASE = "/v1/admin/compliance/incidents"


def _rows(catalog, *, aware=AWARE, classified=None, subs=None, reported=None, closed=False, now=None):
    rows = svc.staged_status(
        catalog, became_aware_at=aware, classified_at=classified,
        stage_submissions={catalog.id: {k: v.isoformat() for k, v in (subs or {}).items()}},
        reported_at=reported, closed=closed, now=now or aware + 1000 * H,
    )
    return {r["rule"]: r for r in rows}


def _at(row):
    return datetime.fromisoformat(row["due_at"]) if row["due_at"] else None


# ---------------------------------------------------------------------------
# Catalog: the stages are the law's
# ---------------------------------------------------------------------------


class TestCatalogStages:
    def test_dora_stages_and_anchors(self):
        assert DORA.deadline_stages == (
            DeadlineStage("initial", "classified", within=4 * H, cap_after_aware=24 * H),
            DeadlineStage("intermediate", "stage:initial", within=72 * H),
            DeadlineStage("final", "stage:intermediate", months=1),
        )

    def test_nis2_stages_and_anchors(self):
        assert NIS2.deadline_stages == (
            DeadlineStage("early_warning", "aware", within=24 * H),
            DeadlineStage("notification", "aware", within=72 * H),
            DeadlineStage("final", "stage:notification", months=1),
        )

    def test_alternatives_regimes_have_no_stages(self):
        for fid in ("eu-ai-act", "gdpr", "hipaa"):
            assert CATALOGS[fid].deadline_stages == (), fid

    def test_stage_must_anchor_on_an_earlier_stage(self):
        from app.services.compliance.catalogs.base import Framework

        with pytest.raises(ValueError):
            Framework(id="x", title="x", jurisdiction="x", reference="x",
                      deadline_stages=(DeadlineStage("a", "stage:b", within=H), DeadlineStage("b", "aware", within=H)))
        with pytest.raises(ValueError):
            DeadlineStage("a", "aware")  # neither within nor months
        with pytest.raises(ValueError):
            DeadlineStage("a", "submitted", within=H)

    def test_summary_carries_the_stages(self):
        assert [s["anchor"] for s in DORA.to_summary()["deadline_stages"]] == [
            "classified", "stage:initial", "stage:intermediate"
        ]


class TestCalendarMonths:
    @pytest.mark.parametrize("start,expected", [
        (datetime(2027, 1, 31, 10, tzinfo=timezone.utc), datetime(2027, 2, 28, 10, tzinfo=timezone.utc)),
        (datetime(2028, 1, 31, 10, tzinfo=timezone.utc), datetime(2028, 2, 29, 10, tzinfo=timezone.utc)),
        (datetime(2026, 12, 15, 9, tzinfo=timezone.utc), datetime(2027, 1, 15, 9, tzinfo=timezone.utc)),
        (datetime(2026, 3, 31, tzinfo=timezone.utc), datetime(2026, 4, 30, tzinfo=timezone.utc)),
    ])
    def test_add_one_month_clamps_the_day(self, start, expected):
        assert add_months(start, 1) == expected


# ---------------------------------------------------------------------------
# DORA
# ---------------------------------------------------------------------------


class TestDoraInitial:
    def test_classified_at_10h_notified_2h_later_is_on_time(self):
        # The regression: measured from awareness this was "reported_late".
        rows = _rows(DORA, classified=AWARE + 10 * H, subs={"initial": AWARE + 12 * H})
        assert _at(rows["initial"]) == AWARE + 14 * H
        assert rows["initial"]["status"] == "reported_on_time"
        assert rows["initial"]["anchor"] == "classified"
        assert rows["initial"]["waiting_for"] is None
        assert all(r["staged"] for r in rows.values())

    def test_same_via_reported_at_fallback_is_on_time(self):
        rows = _rows(DORA, classified=AWARE + 10 * H, reported=AWARE + 12 * H)
        assert rows["initial"]["status"] == "reported_on_time"
        assert rows["initial"]["submitted_at"] == (AWARE + 12 * H).isoformat()

    def test_25h_after_awareness_is_late_even_within_4h_of_classification(self):
        rows = _rows(DORA, classified=AWARE + 22 * H, subs={"initial": AWARE + 25 * H})
        assert _at(rows["initial"]) == AWARE + 24 * H  # min(22 h + 4 h, 24 h)
        assert rows["initial"]["status"] == "reported_late"

    def test_before_classification_only_the_24h_bound_shows(self):
        rows = _rows(DORA, now=AWARE + 2 * H)
        assert _at(rows["initial"]) == AWARE + 24 * H
        assert rows["initial"]["waiting_for"] == "classification"
        assert rows["initial"]["status"] == "due_soon"
        assert rows["intermediate"]["status"] == "waiting" and rows["intermediate"]["due_at"] is None
        assert rows["intermediate"]["waiting_for"] == "initial"
        assert rows["final"]["status"] == "waiting" and rows["final"]["waiting_for"] == "intermediate"

    def test_unclassified_and_unsubmitted_past_24h_is_overdue(self):
        assert _rows(DORA, now=AWARE + 25 * H)["initial"]["status"] == "overdue"


class TestDoraFollowUps:
    def test_intermediate_due_72h_after_initial_submission(self):
        initial = AWARE + 30 * H  # late, but the next clock still runs from it
        rows = _rows(DORA, classified=AWARE + 1 * H, subs={"initial": initial}, now=initial + H)
        assert rows["initial"]["status"] == "reported_late"
        assert _at(rows["intermediate"]) == initial + 72 * H
        assert rows["intermediate"]["status"] == "open"
        assert rows["intermediate"]["anchor_at"] == initial.isoformat()

    @pytest.mark.parametrize("year,day", [(2027, 28), (2028, 29)])
    def test_final_due_one_calendar_month_after_intermediate(self, year, day):
        aware = datetime(year, 1, 28, 9, tzinfo=timezone.utc)
        intermediate = datetime(year, 1, 31, 10, tzinfo=timezone.utc)
        rows = _rows(DORA, aware=aware, classified=aware + H,
                     subs={"initial": aware + 2 * H, "intermediate": intermediate})
        assert _at(rows["final"]) == datetime(year, 2, day, 10, tzinfo=timezone.utc)

    def test_each_stage_is_judged_by_its_own_submission(self):
        initial = AWARE + 3 * H
        rows = _rows(DORA, classified=AWARE + H, subs={
            "initial": initial, "intermediate": initial + 80 * H, "final": initial + 81 * H,
        })
        assert [rows[s]["status"] for s in ("initial", "intermediate", "final")] == [
            "reported_on_time", "reported_late", "reported_on_time",
        ]

    def test_closing_does_not_satisfy_unsubmitted_stages(self):
        rows = _rows(DORA, classified=AWARE + H, subs={"initial": AWARE + 2 * H}, closed=True)
        assert rows["initial"]["status"] == "reported_on_time"
        assert rows["intermediate"]["status"] == "closed_unsubmitted"
        assert rows["final"]["status"] == "closed_unsubmitted"


# ---------------------------------------------------------------------------
# NIS2
# ---------------------------------------------------------------------------


class TestNis2:
    def test_first_two_stages_run_from_awareness(self):
        rows = _rows(NIS2, now=AWARE + H)
        assert _at(rows["early_warning"]) == AWARE + 24 * H
        assert _at(rows["notification"]) == AWARE + 72 * H
        assert rows["final"]["status"] == "waiting" and rows["final"]["waiting_for"] == "notification"

    def test_final_due_one_month_after_notification_submission(self):
        notified = AWARE + 50 * H  # 2026-09-03 10:00
        rows = _rows(NIS2, subs={"early_warning": AWARE + 5 * H, "notification": notified}, now=notified + H)
        assert _at(rows["final"]) == datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
        assert rows["notification"]["status"] == "reported_on_time"
        assert rows["final"]["status"] == "open"

    def test_reported_at_fallback_is_the_early_warning_only(self):
        rows = _rows(NIS2, reported=AWARE + 30 * H)
        assert rows["early_warning"]["status"] == "reported_late"
        assert rows["notification"]["submitted_at"] is None


# ---------------------------------------------------------------------------
# compute_deadlines / deadline_status on a row; alternatives unchanged
# ---------------------------------------------------------------------------


def _incident(frameworks, **kw):
    inc = ComplianceIncident(
        id="i", tenant_id="t", classification=kw.pop("classification", "major_incident"), severity="high",
        title="t", became_aware_at=AWARE, frameworks=frameworks, status=kw.pop("status", "open"),
        classified_at=kw.pop("classified_at", None), stage_submissions=kw.pop("stage_submissions", {}),
        reported_at=kw.pop("reported_at", None),
    )
    inc.deadlines = svc.incident_deadlines(inc)
    return inc


class TestStoredDeadlines:
    def test_stored_snapshot_lists_started_stages(self):
        inc = _incident(["dora", "nis2"])
        assert inc.deadlines["dora"] == {"initial": (AWARE + 24 * H).isoformat()}
        assert set(inc.deadlines["nis2"]) == {"early_warning", "notification"}

    def test_snapshot_follows_the_anchors(self):
        inc = _incident(["dora"], classified_at=AWARE + 2 * H,
                        stage_submissions={"dora": {"initial": (AWARE + 3 * H).isoformat()}})
        assert inc.deadlines["dora"] == {
            "initial": (AWARE + 6 * H).isoformat(), "intermediate": (AWARE + 75 * H).isoformat(),
        }

    def test_legacy_stored_awareness_map_is_not_used_for_staged(self):
        inc = _incident(["dora"], classified_at=AWARE + 10 * H, reported_at=AWARE + 12 * H, status="reported")
        inc.deadlines = {"dora": {"initial": (AWARE + 4 * H).isoformat(),
                                  "intermediate": (AWARE + 72 * H).isoformat()}}
        initial = next(r for r in svc.deadline_status(inc, now=AWARE + 13 * H) if r["rule"] == "initial")
        assert initial["status"] == "reported_on_time"

    def test_waiting_rows_sort_last(self):
        rows = svc.deadline_status(_incident(["dora", "gdpr"]), now=AWARE + H)
        assert [r["due_at"] is None for r in rows] == [False, False, True, True]

    def test_alternatives_unchanged(self):
        d = svc.compute_deadlines(["eu-ai-act", "gdpr", "hipaa"], "death", AWARE, classified_at=AWARE + 5 * H)
        assert d == {
            "eu-ai-act": {"death": (AWARE + timedelta(days=10)).isoformat()},
            "gdpr": {"standard": (AWARE + 72 * H).isoformat()},
            "hipaa": {"standard": (AWARE + timedelta(days=60)).isoformat()},
        }
        inc = _incident(["gdpr"], classification="breach", reported_at=AWARE + 80 * H, status="reported")
        (row,) = svc.deadline_status(inc, now=AWARE + 81 * H)
        assert (row["rule"], row["stage"], row["status"], row["anchor"], row["staged"]) == (
            "standard", "report", "reported_late", "aware", False
        )
        inc.status = "closed"
        (row,) = svc.deadline_status(inc, now=AWARE + 81 * H)
        assert row["status"] == "reported_late"


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _resolver_incident(**kw):
    return {"id": "i", "classification": "major_incident", "became_aware_at": AWARE, "frameworks": ["dora"],
            "deadlines": {"dora": {"initial": (AWARE + 4 * H).isoformat()}}, "status": "reported",
            "reported_at": None, "classified_at": None, "stage_submissions": {}, **kw}


class TestResolvers:
    def test_incident_deadline_for_staged_uses_the_stages(self):
        inc = _resolver_incident(classified_at=AWARE + 10 * H)
        assert incident_deadline(inc, "dora") == AWARE + 14 * H

    def test_on_time_when_every_submitted_stage_was(self):
        inc = _resolver_incident(classified_at=AWARE + 10 * H, reported_at=AWARE + 12 * H)
        assert _staged_outcome(staged_rows(inc, "dora", now=AWARE + 20 * H), AWARE + 20 * H) == "on_time"

    def test_overdue_unsubmitted_stage_counts_against(self):
        inc = _resolver_incident(classified_at=AWARE + 10 * H, reported_at=AWARE + 12 * H)
        now = AWARE + 12 * H + 73 * H  # intermediate due 72 h after the initial, not submitted
        assert _staged_outcome(staged_rows(inc, "dora", now=now), now) == "late"

    def test_closed_with_overdue_unsubmitted_stage_counts_against(self):
        inc = _resolver_incident(classified_at=AWARE + 10 * H, reported_at=AWARE + 12 * H, status="closed")
        now = AWARE + 200 * H
        assert _staged_outcome(staged_rows(inc, "dora", now=now), now) == "late"

    def test_nothing_submitted_nothing_due_is_pending(self):
        inc = _resolver_incident(status="open")
        assert _staged_outcome(staged_rows(inc, "dora", now=AWARE + H), AWARE + H) == "pending"

    def test_alternatives_are_not_staged(self):
        assert staged_rows(_resolver_incident(), "gdpr", now=AWARE) is None


# ---------------------------------------------------------------------------
# HTTP: classified_at, submissions, refusals
# ---------------------------------------------------------------------------


def seed(session):
    session.add(ResourceGroup(
        id="sys-stage", tenant_id="t1", name="sys-stage", description="s", hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
    ))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_compliance_incidents_router(BrutorLogger("incident-stage-tests"))])
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def core(monkeypatch):
    return fakes.install(monkeypatch)


def _create(harness, **over):
    body = {
        "title": "Payment ICT outage", "classification": "major_incident", "severity": "high",
        "became_aware_at": AWARE.isoformat(), "frameworks": ["dora", "gdpr"], "ai_system_id": "sys-stage",
        **over,
    }
    return harness.client.post(BASE, json=body, headers=harness.headers())


def _submit(harness, iid, stage, at, framework_id="dora"):
    return harness.client.post(
        f"{BASE}/{iid}/submissions",
        json={"framework_id": framework_id, "stage": stage, "submitted_at": at.isoformat()},
        headers=harness.headers(),
    )


def _instant(value):
    """The test harness is SQLite, which drops the zone on read."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row(incident, fid, rule):
    return next(r for r in incident["deadline_status"] if r["framework_id"] == fid and r["rule"] == rule)


class TestSubmissionHttp:
    def test_staged_lifecycle_is_sealed_and_chained(self, harness, core):
        r = _create(harness, classified_at=(AWARE + 10 * H).isoformat())
        assert r.status_code == 201, r.text
        inc = r.json()["incident"]
        assert _instant(inc["classified_at"]) == AWARE + 10 * H
        assert inc["stage_submissions"] == {}
        assert _row(inc, "dora", "initial")["due_at"] == (AWARE + 14 * H).isoformat()
        assert _row(inc, "dora", "intermediate")["status"] == "waiting"

        r = _submit(harness, inc["id"], "initial", AWARE + 12 * H)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event"]["event"] == "stage_submitted"
        assert body["event"]["detail"] == {
            "framework_id": "dora", "stage": "initial", "submitted_at": (AWARE + 12 * H).isoformat(),
            "due_at": (AWARE + 14 * H).isoformat(), "status": "reported_on_time",
        }
        inc = body["incident"]
        assert inc["stage_submissions"] == {"dora": {"initial": (AWARE + 12 * H).isoformat()}}
        assert _row(inc, "dora", "intermediate")["due_at"] == (AWARE + 84 * H).isoformat()
        assert inc["deadlines"]["dora"]["intermediate"] == (AWARE + 84 * H).isoformat()

        r = _submit(harness, inc["id"], "intermediate", AWARE + 80 * H)
        assert r.status_code == 200, r.text
        final = _row(r.json()["incident"], "dora", "final")
        assert final["due_at"] == datetime(2026, 10, 4, 16, tzinfo=timezone.utc).isoformat()

        records = [x for x in core.records("ai.brutor.incident") if x["context"]["incident_id"] == inc["id"]]
        assert [x["context"]["state"] for x in records] == ["created", "stage_submitted", "stage_submitted"]
        assert all([ref["purpose"] for ref in x.get("references") or []] == ["previous_state"] for x in records[1:])
        assert records[-1]["payload"]["stage_submissions"]["dora"].keys() == {"initial", "intermediate"}

    def test_refusals(self, harness):
        iid = _create(harness).json()["incident"]["id"]
        assert _submit(harness, iid, "initial", AWARE + H, framework_id="nis2").status_code == 422  # not in scope
        assert _submit(harness, iid, "standard", AWARE + H, framework_id="gdpr").status_code == 422  # not staged
        assert _submit(harness, iid, "nope", AWARE + H).status_code == 422  # unknown stage
        assert _submit(harness, iid, "initial", AWARE - H).status_code == 422  # before awareness
        assert _submit(harness, iid, "intermediate", AWARE + 5 * H).status_code == 409  # anchor unsubmitted
        future = datetime.now(timezone.utc) + timedelta(days=2)
        assert _submit(harness, iid, "initial", future).status_code == 422
        assert _submit(harness, iid, "initial", AWARE + 5 * H).status_code == 200
        assert _submit(harness, iid, "initial", AWARE + 6 * H).status_code == 409  # already recorded
        assert _submit(harness, iid, "intermediate", AWARE + 4 * H).status_code == 422  # before its anchor
        assert harness.client.post(f"{BASE}/{iid}/close", json={}, headers=harness.headers()).status_code == 200
        assert _submit(harness, iid, "intermediate", AWARE + 9 * H).status_code == 409  # closed
        detail = harness.client.get(f"{BASE}/{iid}", headers=harness.headers()).json()
        assert _row(detail, "dora", "intermediate")["status"] == "closed_unsubmitted"

    def test_submission_before_classification_is_refused(self, harness):
        iid = _create(harness, classified_at=(AWARE + 10 * H).isoformat()).json()["incident"]["id"]
        assert _submit(harness, iid, "initial", AWARE + 9 * H).status_code == 422

    def test_classified_at_validation_and_recompute(self, harness):
        assert _create(harness, classified_at=(AWARE - H).isoformat()).status_code == 422
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        assert _create(harness, classified_at=future).status_code == 422
        iid = _create(harness).json()["incident"]["id"]
        r = harness.client.patch(f"{BASE}/{iid}", json={"classified_at": (AWARE + 2 * H).isoformat()},
                                 headers=harness.headers())
        assert r.status_code == 200, r.text
        inc = r.json()["incident"]
        assert inc["deadlines"]["dora"] == {"initial": (AWARE + 6 * H).isoformat()}
        assert r.json()["event"]["detail"] == {"changed": {"classified_at": (AWARE + 2 * H).isoformat()}}
        assert _submit(harness, iid, "initial", AWARE + 3 * H).status_code == 200
        # Moving classification after the recorded initial notification is refused, and changes nothing.
        r = harness.client.patch(f"{BASE}/{iid}", json={"classified_at": (AWARE + 4 * H).isoformat()},
                                 headers=harness.headers())
        assert r.status_code == 422
        r = harness.client.patch(f"{BASE}/{iid}", json={"became_aware_at": (AWARE + 3 * H).isoformat()},
                                 headers=harness.headers())
        assert r.status_code == 422  # would put classification before awareness
        detail = harness.client.get(f"{BASE}/{iid}", headers=harness.headers()).json()
        assert _instant(detail["classified_at"]) == AWARE + 2 * H
        assert _instant(detail["became_aware_at"]) == AWARE


# ---------------------------------------------------------------------------
# The incident-stages migration (static shape; up/down was run on a throwaway Postgres)
# ---------------------------------------------------------------------------
