# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Drift resolution that the detector honours, behavioural spec

Real app, real auth, full-schema database (the shared admin harness; SQLite,
or a throwaway Postgres with BRUTOR_PG_TESTS=1). One class per rule, each
guarding the CLASS of bug:

  * Closing needs a kind (422 without it) and a note; each kind has exactly
    one effect (epoch at the change point / incident window / epoch at the
    detection time); a closed finding cannot be closed again (409); ack is
    unchanged and never closes.
  * One closer — no module but `app/services/drift_resolution.py` closes a
    finding (static), and the ORM refuses to flush a finding closed without
    a kind (runtime). Every automatic closer sets `auto_cleared`.
  * Incident windows — CRUD per system and tenant-wide; every declaration,
    edit and withdrawal is sealed; a failed seal leaves the row unsealed and
    raises a capture gap.
  * Response policies — a closed finding stops being a trigger; the
    evaluator never fires on evidence inside a window or at/before an epoch;
    a restore after resolution is not undone by the next tick (real
    evaluator + real scheduler tick), while new evidence still fires.
  * The report lists windows and epochs, and coverage states the excluded
    period (spec 1.8, validated against the vendored schema).

The dump of the demo incident is scenario
`TestRestoreSticks.test_the_demo_incident_does_not_re_demote`.
"""

from __future__ import annotations

import ast
import os
import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.ai_system_drift import create_ai_system_drift_router
from app.api.v1.endpoints.admin.ai_system_incident_windows import (
    create_ai_system_incident_windows_router,
)
from app.api.v1.endpoints.admin.ai_system_response import create_ai_system_response_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_contract import AISystemContract, hash_closure
from app.database.models.ai_system_drift import (
    AISystemDriftEvent,
    DriftClosedWithoutKindError,
)
from app.database.models.ai_system_drift_resolution import AISystemIncidentWindow
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.evidence import core_client
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database
from tests.compliance_evidence_fakes import MODE_OK, MODE_UNREACHABLE, FakeCore
from tests.source_trees import python_sources, source_label

T1 = "t1"
_n = count(1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


@pytest.fixture(scope="module")
def database_url():
    yield from throwaway_pg_database("rfc24cp")


@pytest.fixture(scope="module")
def fake_core():
    mp = pytest.MonkeyPatch()
    fake = FakeCore()
    mp.setattr(core_client, "seal", fake.seal)
    yield fake
    mp.undo()


@pytest.fixture(scope="module")
def h(database_url, fake_core):
    log = BrutorLogger("drift-resolution-tests")
    harness = build_admin_harness(
        [
            create_ai_system_drift_router(log),
            create_ai_system_incident_windows_router(log),
            create_ai_system_response_router(log),
        ],
        logger_name="drift-resolution-tests",
        database_url=database_url,
    )
    yield harness
    harness.close()


@pytest.fixture(autouse=True)
def _core_ok(fake_core):
    fake_core.mode = MODE_OK
    yield


@pytest.fixture(autouse=True)
def _no_tenant_wide_leftovers(h):
    """A tenant-wide window covers EVERY system of the tenant — exactly its
    point — so one test's window must not judge the next test's runs."""
    yield
    h.seed(lambda s: s.query(AISystemIncidentWindow).filter(
        AISystemIncidentWindow.ai_system_id.is_(None)).delete())


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def system(h, gid=None, *, autonomy=None, tenant=T1):
    gid = gid or f"sys-{next(_n)}"
    h.seed(lambda s: s.add(ResourceGroup(
        id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent", owner="team",
        intended_use="x", lifecycle_stage="active", autonomy_level=autonomy,
    )))
    return gid


def finding(h, sid, *, metric="action_count", drift_class="behavioural", severity="high",
            change_point=None, detected=None, caller=None, basis="runs", tenant=T1):
    eid = f"drift-{next(_n)}"
    detected = detected or _now() - timedelta(minutes=5)
    h.seed(lambda s: s.add(AISystemDriftEvent(
        id=eid, tenant_id=tenant, ai_system_id=sid, drift_class=drift_class,
        metric_key=metric, basis=basis, caller_system_id=caller, severity=severity,
        attributed_cause="unexplained", detected_at=detected, change_point_at=change_point,
        status="active",
    )))
    return eid


def run(h, sid, *, at, state="completed", **extra):
    rid = f"run-{next(_n)}"
    if state in ("completed", "completed_degraded"):
        # A completed run is a success only with a declared work outcome.
        extra = {"outcome_id": "task_completed", "outcome_conformance": "declared",
                 "outcome_counts_toward_baseline": True, **extra}
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
        started_at=at, ended_at=at + timedelta(seconds=10), terminal_state=state,
        finalized_by="explicit", is_replay=False, action_count=3, llm_call_count=1,
        chain_integrity="intact", **extra,
    )))
    return rid


def policy(h, sid, *, level="approval_required", drift_class=None):
    pid = f"pol-{next(_n)}"
    h.seed(lambda s: s.add(AISystemResponsePolicy(
        id=pid, tenant_id=T1, ai_system_id=sid, name=pid, enabled=True,
        signal_kind="drift", severity_at_least="high", drift_class=drift_class,
        actions=[{"kind": "set_autonomy", "level": level}], require_human_to_restore=True,
        response_cooldown_seconds=0,
    )))
    return pid


def _t2_window(h):
    sid = system(h, tenant="t2")
    wid = f"iw-t2-{next(_n)}"
    h.seed(lambda s: s.add(AISystemIncidentWindow(
        id=wid, tenant_id="t2", ai_system_id=None, starts_at=_now() - timedelta(hours=5),
        ends_at=_now(), cause_category="other", reason="theirs")))
    return wid


def close(h, eid, kind, *, note="because", route="resolve", tenant=T1, **extra):
    body = {"kind": kind, "note": note, **extra}
    return h.client.post(f"/v1/admin/drift/{eid}/{route}", json=body, headers=h.headers(tenant))


def event_row(h, eid):
    s = h.Factory()
    try:
        return s.get(AISystemDriftEvent, eid)
    finally:
        s.close()


def autonomy(h, sid):
    s = h.Factory()
    try:
        return s.get(ResourceGroup, sid).autonomy_level
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Closing needs a kind; each kind has one effect
# ---------------------------------------------------------------------------


class TestCloseNeedsAKind:
    def test_resolve_without_kind_is_422(self, h):
        eid = finding(h, system(h))
        r = h.client.post(f"/v1/admin/drift/{eid}/resolve", json={"note": "x"},
                          headers=h.headers(T1))
        assert r.status_code == 422
        assert event_row(h, eid).status == "active"

    def test_an_empty_body_is_422_on_both_close_routes(self, h):
        eid = finding(h, system(h))
        for route in ("resolve", "false-positive"):
            r = h.client.post(f"/v1/admin/drift/{eid}/{route}", json={}, headers=h.headers(T1))
            assert r.status_code == 422, route
        assert event_row(h, eid).is_open

    def test_a_note_is_required(self, h):
        eid = finding(h, system(h))
        assert close(h, eid, "accepted_change", note="").status_code == 422
        assert h.client.post(f"/v1/admin/drift/{eid}/resolve", json={"kind": "cause_fixed"},
                             headers=h.headers(T1)).status_code == 422

    def test_auto_cleared_is_not_a_human_kind(self, h):
        eid = finding(h, system(h))
        assert close(h, eid, "auto_cleared").status_code == 422

    def test_the_service_refuses_auto_cleared_from_a_human_too(self, h):
        """Defence in depth under the API Literal: the service is the rule."""
        from app.services import drift_resolution

        eid = finding(h, system(h))
        s = h.Factory()
        try:
            with pytest.raises(drift_resolution.ResolutionError):
                drift_resolution.resolve_finding(
                    s, s.get(AISystemDriftEvent, eid), kind="auto_cleared", by="x", note="n")
        finally:
            s.rollback()
            s.close()
        assert event_row(h, eid).is_open

    def test_false_positive_route_takes_only_false_positive(self, h):
        eid = finding(h, system(h))
        r = close(h, eid, "accepted_change", route="false-positive")
        assert r.status_code == 422
        assert event_row(h, eid).is_open

    def test_window_bounds_belong_to_cause_fixed_only(self, h):
        eid = finding(h, system(h))
        r = close(h, eid, "accepted_change", window_ends_at=_now().isoformat())
        assert r.status_code == 422

    def test_a_closed_finding_cannot_be_closed_again(self, h):
        eid = finding(h, system(h))
        assert close(h, eid, "accepted_change").status_code == 200
        assert close(h, eid, "cause_fixed").status_code == 409
        assert event_row(h, eid).resolution_kind == "accepted_change"

    def test_ack_is_unchanged_and_never_closes(self, h):
        eid = finding(h, system(h))
        r = h.client.post(f"/v1/admin/drift/{eid}/ack", json={}, headers=h.headers(T1))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "acknowledged" and body["is_open"] is True
        assert body["resolution_kind"] is None
        # …and an acknowledged finding still closes with a kind.
        assert close(h, eid, "cause_fixed").status_code == 200

    def test_another_tenant_cannot_close_it(self, h):
        eid = finding(h, system(h))
        assert close(h, eid, "accepted_change", tenant="t2").status_code == 404


class TestEachKindHasOneEffect:
    def test_accepted_change_starts_an_epoch_at_the_change_point(self, h, fake_core):
        sid = system(h)
        cp = _now() - timedelta(days=2)
        eid = finding(h, sid, metric="action_count", change_point=cp)
        r = close(h, eid, "accepted_change", note="the 09-26 retune")
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["status"], body["resolution_kind"]) == ("resolved", "accepted_change")
        epoch = body["baseline_epoch"]
        assert body["incident_window"] is None
        assert (epoch["metric_key"], epoch["basis"], epoch["caller_system_id"], epoch["kind"]) == (
            "action_count", "runs", "all", "accepted_change")
        assert _aware(datetime.fromisoformat(epoch["starts_at"])) == cp
        assert epoch["drift_event_id"] == eid and epoch["declared_by"] == "admin_t1"
        assert body["seal"]["sealed"] is True and epoch["record_id"] == body["seal"]["record_id"]
        (rec,) = [r for r in fake_core.records() if r["context"].get("state") == "drift_resolution"
                  and r["payload"]["drift_event_id"] == eid]
        assert rec["record_type"] == "ai.brutor.declaration"
        assert rec["payload"]["resolution_kind"] == "accepted_change"
        assert rec["payload"]["effect"]["baseline_epoch"]["id"] == epoch["id"]
        assert rec["disposition"]["authority"] == epoch["id"]
        # Never-enters: no admin identity in the record.
        assert "admin_t1" not in repr(rec)

    def test_accepted_change_without_change_point_falls_back_to_detection(self, h):
        sid = system(h)
        det = _now() - timedelta(hours=3)
        eid = finding(h, sid, detected=det)
        epoch = close(h, eid, "accepted_change").json()["baseline_epoch"]
        assert _aware(datetime.fromisoformat(epoch["starts_at"])) == det

    def test_a_per_caller_finding_epochs_its_caller(self, h):
        sid, caller = system(h), system(h)
        eid = finding(h, sid, basis="segments", caller=caller, change_point=_now() - timedelta(hours=1))
        epoch = close(h, eid, "accepted_change").json()["baseline_epoch"]
        assert (epoch["basis"], epoch["caller_system_id"]) == ("segments", caller)

    def test_cause_fixed_declares_a_window_from_change_point_to_now(self, h, fake_core):
        sid = system(h)
        cp = _now() - timedelta(hours=10)
        eid = finding(h, sid, change_point=cp)
        before = _now()
        body = close(h, eid, "cause_fixed", note="provider credits ran out").json()
        w = body["incident_window"]
        assert body["baseline_epoch"] is None
        assert (body["status"], body["resolution_kind"]) == ("resolved", "cause_fixed")
        assert _aware(datetime.fromisoformat(w["starts_at"])) == cp
        assert _aware(datetime.fromisoformat(w["ends_at"])) >= before
        assert (w["ai_system_id"], w["tenant_wide"], w["cause_category"], w["reason"]) == (
            sid, False, "other", "provider credits ran out")
        assert w["drift_event_id"] == eid and w["record_id"]
        assert any(r["payload"].get("drift_event_id") == eid
                   and "incident_window" in r["payload"]["effect"] for r in fake_core.records())

    def test_cause_fixed_bounds_category_and_scope_are_editable(self, h):
        sid = system(h)
        eid = finding(h, sid, change_point=_now() - timedelta(hours=10))
        start, end = _now() - timedelta(hours=12), _now() - timedelta(hours=2)
        w = close(h, eid, "cause_fixed", window_starts_at=start.isoformat(),
                  window_ends_at=end.isoformat(), cause_category="provider_outage",
                  tenant_wide=True).json()["incident_window"]
        assert _aware(datetime.fromisoformat(w["starts_at"])) == start
        assert _aware(datetime.fromisoformat(w["ends_at"])) == end
        assert (w["cause_category"], w["ai_system_id"], w["tenant_wide"]) == (
            "provider_outage", None, True)

    def test_grouped_findings_attach_to_one_window(self, h, fake_core):
        """N identical findings of one incident: the first declares the
        window, the rest attach — one window, listing every finding it closed."""
        sid = system(h)
        cp = _now() - timedelta(hours=10)
        first, *rest = [finding(h, sid, change_point=cp) for _ in range(3)]
        w = close(h, first, "cause_fixed", note="outage").json()["incident_window"]
        for eid in rest:
            r = close(h, eid, "cause_fixed", note="outage", incident_window_id=w["id"])
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["incident_window"]["id"] == w["id"]
            assert body["resolution_kind"] == "cause_fixed" and body["seal"]["sealed"]
        listed = h.client.get(f"/v1/admin/ai-systems/{sid}/incident-windows",
                              headers=h.headers(T1)).json()["windows"]
        assert [x["id"] for x in listed] == [w["id"]]
        assert listed[0]["drift_event_ids"] == [first, *rest]
        attached = [x for x in fake_core.records() if x["context"].get("state") == "drift_resolution"
                    and x["payload"]["drift_event_id"] in rest]
        assert len(attached) == 2 and all(x["payload"]["effect"]["attached"] for x in attached)
        from app.database.models.ai_system_drift_resolution import AISystemIncidentWindowFinding as L

        s = h.Factory()
        try:
            links = {l.drift_event_id: (l.role, bool(l.record_id))
                     for l in s.query(L).filter_by(incident_window_id=w["id"])}
            # The window keeps the record that DECLARED it.
            assert s.get(AISystemIncidentWindow, w["id"]).record_id == w["record_id"]
        finally:
            s.close()
        assert links == {first: ("declared", True), rest[0]: ("attached", True),
                         rest[1]: ("attached", True)}

    @pytest.mark.parametrize("case", ["other_system", "other_tenant", "missing", "with_bounds",
                                      "not_cause_fixed"])
    def test_attaching_is_422_unless_the_window_covers_the_finding(self, h, case):
        sid, other = system(h), system(h)
        eid = finding(h, sid)
        target = {
            "other_system": lambda: declare(h, sid=other).json()["id"],
            "other_tenant": lambda: _t2_window(h),
            "missing": lambda: "iw-nope",
            "with_bounds": lambda: declare(h, sid=sid).json()["id"],
            "not_cause_fixed": lambda: declare(h, sid=sid).json()["id"],
        }[case]()
        extra = {"window_ends_at": _now().isoformat()} if case == "with_bounds" else {}
        kind = "accepted_change" if case == "not_cause_fixed" else "cause_fixed"
        r = close(h, eid, kind, incident_window_id=target, **extra)
        assert r.status_code == 422, r.text
        assert event_row(h, eid).is_open

    def test_a_tenant_wide_window_takes_any_system(self, h):
        sid = system(h)
        wide = declare(h, tenant_wide=True).json()["id"]
        body = close(h, finding(h, sid), "cause_fixed", incident_window_id=wide).json()
        assert body["incident_window"]["id"] == wide

    def test_the_report_lists_each_window_once_with_its_findings(self, h):
        from app.services.ai_system_assurance import build_report

        sid = system(h)
        cp = _now() - timedelta(hours=5)
        a, b = finding(h, sid, change_point=cp), finding(h, sid, change_point=cp)
        wid = close(h, a, "cause_fixed").json()["incident_window"]["id"]
        close(h, b, "cause_fixed", incident_window_id=wid)
        s = h.Factory()
        try:
            windows = build_report(s, s.get(ResourceGroup, sid))["judgement_exclusions"]["incident_windows"]
        finally:
            s.close()
        assert [(w["id"], w["drift_event_ids"]) for w in windows] == [(wid, [a, b])]

    def test_cause_fixed_with_inverted_bounds_is_422_and_nothing_changes(self, h):
        sid = system(h)
        eid = finding(h, sid)
        r = close(h, eid, "cause_fixed", window_starts_at=_now().isoformat(),
                  window_ends_at=(_now() - timedelta(hours=1)).isoformat())
        assert r.status_code == 422
        assert event_row(h, eid).is_open

    def test_false_positive_epochs_the_detection_time(self, h):
        sid = system(h)
        det = _now() - timedelta(hours=1)
        eid = finding(h, sid, change_point=_now() - timedelta(days=3), detected=det)
        body = close(h, eid, "false_positive", route="false-positive").json()
        assert (body["status"], body["resolution_kind"]) == ("false_positive", "false_positive")
        epoch = body["baseline_epoch"]
        assert epoch["kind"] == "false_positive"
        assert _aware(datetime.fromisoformat(epoch["starts_at"])) == det

    def test_false_positive_through_resolve_is_the_same_fact(self, h):
        eid = finding(h, system(h))
        body = close(h, eid, "false_positive").json()
        assert (body["status"], body["resolution_kind"]) == ("false_positive", "false_positive")

    def test_a_failed_seal_saves_the_resolution_unsealed_and_raises_a_gap(self, h, fake_core):
        sid = system(h)
        eid = finding(h, sid)
        fake_core.mode = MODE_UNREACHABLE
        body = close(h, eid, "accepted_change").json()
        assert body["resolution_kind"] == "accepted_change"
        assert body["seal"]["sealed"] is False and body["seal"]["seal_error"] == "core_unreachable"
        assert body["baseline_epoch"]["record_id"] is None
        s = h.Factory()
        try:
            gap = s.query(AssuranceInboxItem).filter(
                AssuranceInboxItem.tenant_id == T1,
                AssuranceInboxItem.category == "capture_gap",
                AssuranceInboxItem.ai_system_id == sid,
            ).first()
            assert gap is not None
        finally:
            s.close()

    def test_the_list_shows_the_kind_and_baselines_show_their_epoch(self, h):
        from app.database.models.ai_system_drift import AISystemBaseline

        sid = system(h)
        h.seed(lambda s: s.add(AISystemBaseline(
            id=f"base-{next(_n)}", tenant_id=T1, ai_system_id=sid, metric_key="action_count",
            bucket="all", actor_id="all", basis="runs", caller_system_id="all",
            median=1.0, mad=0.1, sample_count=3, state="learning",
        )))
        eid = finding(h, sid, change_point=_now() - timedelta(days=1))
        close(h, eid, "accepted_change")
        events = h.client.get(f"/v1/admin/ai-systems/{sid}/drift?status=resolved",
                              headers=h.headers(T1)).json()["events"]
        assert [e["resolution_kind"] for e in events] == ["accepted_change"]
        (b,) = h.client.get(f"/v1/admin/ai-systems/{sid}/baselines",
                            headers=h.headers(T1)).json()["baselines"]
        assert b["epoch"]["kind"] == "accepted_change" and b["epoch"]["drift_event_id"] == eid
        epochs = h.client.get(f"/v1/admin/ai-systems/{sid}/baseline-epochs",
                              headers=h.headers(T1)).json()
        assert epochs["total"] == 1


# ---------------------------------------------------------------------------
# One closer
# ---------------------------------------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app"
CLOSER = APP / "services" / "drift_resolution.py"
_CLOSED_LITERALS = {"resolved", "false_positive"}
_CLOSED_NAMES = {"DRIFT_RESOLVED", "DRIFT_FALSE_POSITIVE"}


def _closer_violations(path: Path) -> list:
    """Assignments that close a drift finding outside the closer: `x.status
    = <closed>` / `x.resolution_kind = …` in a module that touches drift
    events, `status=<closed>` passed to AISystemDriftEvent(...), a bulk
    `.update({... "status": <closed>})` on it, or raw SQL updating the table."""
    text = path.read_text()
    tree = ast.parse(text, str(path))
    touches_drift = "AISystemDriftEvent" in text or "ai_system_drift" in text
    out = []

    def closed(v) -> bool:
        return (isinstance(v, ast.Constant) and v.value in _CLOSED_LITERALS) or (
            isinstance(v, ast.Name) and v.id in _CLOSED_NAMES)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and touches_drift:
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "status" and closed(node.value):
                    out.append(f"{path.name}:{node.lineno} sets .status to a closed value")
                if isinstance(t, ast.Attribute) and t.attr == "resolution_kind":
                    out.append(f"{path.name}:{node.lineno} sets .resolution_kind")
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "AISystemDriftEvent":
                for kw in node.keywords:
                    if kw.arg in ("status", "resolution_kind") and (
                        kw.arg == "resolution_kind" or closed(kw.value)
                    ):
                        out.append(f"{path.name}:{node.lineno} constructs a closed finding")
            if name == "update" and touches_drift:
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for k, v in zip(arg.keys, arg.values):
                            key = getattr(k, "value", None) or getattr(k, "attr", None)
                            if key in ("status", "resolution_kind") and (
                                key == "resolution_kind" or closed(v)
                            ):
                                out.append(f"{path.name}:{node.lineno} bulk-closes findings")
    if re.search(r"UPDATE\s+ai_system_drift_events\s+SET[^;]*\b(status|resolution_kind)\b",
                 text, re.I):
        out.append(f"{path.name}: raw SQL closes findings")
    return out


class TestOneCloser:
    def test_only_the_closer_closes_findings(self):
        found = []
        for path in python_sources(APP):
            if path == CLOSER:
                continue
            found.extend(_closer_violations(path))
        assert not found, (
            "Close drift findings through app.services.drift_resolution "
            "(close_finding / auto_clear / resolve_finding):\n  " + "\n  ".join(found)
        )

    def test_the_guard_catches_every_old_shape(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text(
            "from app.database.models.ai_system_drift import AISystemDriftEvent, DRIFT_RESOLVED\n"
            "def f(ev, db):\n"
            "    ev.status = 'resolved'\n"
            "    ev.status = DRIFT_RESOLVED\n"
            "    ev.resolution_kind = 'auto_cleared'\n"
            "    AISystemDriftEvent(id='x', status='false_positive')\n"
            "    db.query(AISystemDriftEvent).update({'status': 'resolved'})\n"
            "    db.execute('UPDATE ai_system_drift_events SET status = 1')\n"
        )
        assert len(_closer_violations(bad)) == 6, _closer_violations(bad)

    def test_the_closer_is_actually_exercised(self):
        assert "def close_finding" in CLOSER.read_text()

    def test_the_orm_refuses_a_close_without_a_kind(self, h):
        sid = system(h)
        eid = finding(h, sid)
        s = h.Factory()
        try:
            s.get(AISystemDriftEvent, eid).status = "resolved"
            with pytest.raises(DriftClosedWithoutKindError):
                s.flush()
            s.rollback()
            s.add(AISystemDriftEvent(
                id=f"drift-{next(_n)}", tenant_id=T1, ai_system_id=sid, drift_class="cost",
                metric_key="x", status="false_positive"))
            with pytest.raises(DriftClosedWithoutKindError):
                s.flush()
        finally:
            s.rollback()
            s.close()

    def test_a_legacy_closed_row_can_still_be_touched(self, h):
        """Rows closed before `a192_drift_resolution` carry NULL: stamping them (response_taken,
        alert id) is not a close, and must not be refused."""
        sid = system(h)
        eid = finding(h, sid)
        from sqlalchemy import text

        h.seed(lambda s: s.execute(
            text("UPDATE ai_system_drift_events SET status = 'resolved' WHERE id = :i"),
            {"i": eid}))
        s = h.Factory()
        try:
            s.get(AISystemDriftEvent, eid).system_alert_id = "alert-1"
            s.commit()
        finally:
            s.close()

    def test_the_database_refuses_an_open_finding_with_a_kind(self, h):
        from sqlalchemy.exc import IntegrityError

        sid = system(h)
        s = h.Factory()
        try:
            s.add(AISystemDriftEvent(
                id=f"drift-{next(_n)}", tenant_id=T1, ai_system_id=sid, drift_class="cost",
                metric_key="x", status="active", resolution_kind="cause_fixed"))
            with pytest.raises(IntegrityError):
                s.flush()
        finally:
            s.rollback()
            s.close()


class TestEveryAutomaticCloserClearsWithAutoCleared:
    def test_a_healed_envelope_breach(self, h):
        from app.services.ai_system_envelope import emit_envelope_violations

        sid = system(h)
        s = h.Factory()
        try:
            sys_row = s.get(ResourceGroup, sid)
            breached = {"terms": [{"term": "min_completion_rate", "label": "x", "status": "breached",
                                   "severity": "high", "declared": 0.9, "observed": 0.5,
                                   "direction": "min", "detail": "d"}],
                        "evaluated_runs": 30, "period_hours": 720}
            emit_envelope_violations(s, sys_row, breached)
            s.commit()
            met = {**breached, "terms": [{**breached["terms"][0], "status": "met"}]}
            emit_envelope_violations(s, sys_row, met)
            s.commit()
            (ev,) = s.query(AISystemDriftEvent).filter_by(ai_system_id=sid).all()
            assert (ev.status, ev.resolution_kind, ev.resolved_by) == (
                "resolved", "auto_cleared", "system:envelope-evaluator")
        finally:
            s.close()

    def test_a_dependency_that_recovered(self, h):
        from app.services.ai_system_dependency_health import emit_dependency_findings

        root, svc = system(h), system(h)
        entry = {"edge_id": "e1", "system_id": svc, "name": svc, "criticality": "critical",
                 "status": "degraded", "failing_signal": "liveness", "via_system_id": None,
                 "role": "shared_service", "caps_root": True}
        s = h.Factory()
        try:
            r = s.get(ResourceGroup, root)
            emit_dependency_findings(s, r, [entry])
            s.commit()
            emit_dependency_findings(s, r, [])
            s.commit()
            (ev,) = s.query(AISystemDriftEvent).filter_by(ai_system_id=root).all()
            assert (ev.status, ev.resolution_kind) == ("resolved", "auto_cleared")
        finally:
            s.close()

    def test_a_superseded_workload_class_finding(self, h):
        from app.services.ai_system_assurance import _emit_class_change

        sid = system(h)
        s = h.Factory()
        try:
            r = s.get(ResourceGroup, sid)
            _emit_class_change(s, r, "T(n)", "T(n·k)", {})
            s.commit()
            _emit_class_change(s, r, "T(n·k)", "T(n·k·a)", {})
            s.commit()
            rows = s.query(AISystemDriftEvent).filter_by(ai_system_id=sid).order_by(
                AISystemDriftEvent.detected_at).all()
            closed = [e for e in rows if not e.is_open]
            assert [(e.status, e.resolution_kind) for e in closed] == [("resolved", "auto_cleared")]
        finally:
            s.close()


class TestConditionEmittersHonourTheResolution:
    """The control plane's own condition findings (dependency, agent) are
    re-evaluated every tick. A human resolution must not be answered by the
    same finding re-opened from the same evidence on the next tick."""

    def _segment(self, h, root, svc, at, outcome):
        from app.database.models.ai_system_run_segment import AISystemRunSegment

        rid = run(h, root, at=at)
        h.seed(lambda s: s.add(AISystemRunSegment(
            id=f"seg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
            root_system_id=root, caller_system_id=root, segment_system_id=svc, min_depth=1,
            first_action_at=at, last_action_at=at + timedelta(seconds=5), action_count=1,
            llm_call_count=1, cost_usd=0.001, outcome=outcome, models_used={})))

    def _emit(self, h, root, svc):
        from app.services.ai_system_dependency_health import emit_dependency_findings

        entry = {"edge_id": "e1", "system_id": svc, "name": svc, "criticality": "critical",
                 "status": "degraded", "failing_signal": "liveness", "via_system_id": None,
                 "role": "shared_service", "caps_root": True}
        s = h.Factory()
        try:
            emit_dependency_findings(s, s.get(ResourceGroup, root), [entry])
            s.commit()
            return [(e.id, e.status) for e in s.query(AISystemDriftEvent).filter_by(
                ai_system_id=root).order_by(AISystemDriftEvent.detected_at)]
        finally:
            s.close()

    def test_an_accepted_dependency_finding_is_not_re_opened_from_the_same_calls(self, h):
        root, svc = system(h), system(h)
        self._segment(h, root, svc, _now() - timedelta(hours=2), "timed_out")
        ((fid, _),) = self._emit(h, root, svc)
        assert close(h, fid, "accepted_change", note="the service is retired").status_code == 200
        assert self._emit(h, root, svc) == [(fid, "resolved")]
        # A new failing call after the resolution is new evidence.
        self._segment(h, root, svc, _now() + timedelta(seconds=1), "timed_out")
        rows = self._emit(h, root, svc)
        assert len(rows) == 2 and rows[-1][1] == "active"

    def test_an_accepted_agent_finding_is_not_re_opened_for_the_same_release(self, h):
        from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
        from app.database.models.agent_implementation_release import AgentImplementationRelease
        from app.services.assurance_checks.agent import run_agent_checks

        sid = system(h)
        aid = f"agent-{next(_n)}"

        def seed(s):
            s.add(AgentIdentity(id=aid, tenant_id=T1, name=aid, enabled=True, status="active",
                                default_deny=True, enforcement_mode="enforce",
                                approved_versions=["1.*"]))
            s.flush()
            s.add(ResourceGroupAgentMember(tenant_id=T1, agent_id=aid, group_id=sid,
                                           role="component",
                                           joined_at=_now() - timedelta(days=30)))
        h.seed(seed)

        def release(version, first):
            # Ids sort OLDEST first on purpose: the finding merges every
            # unapproved release, and the newest one's evidence must count.
            h.seed(lambda s: s.add(AgentImplementationRelease(
                id=f"rel-{version}-{aid}", tenant_id=T1, agent_id=aid, name=aid, version=version,
                build="", trust="declared", first_seen_at=first, last_seen_at=_now(),
                run_count=1, fingerprints=["sha256:" + "a" * 64])))
            # A release is judged by its
            # runs: the finding's evidence is the first unapproved run.
            run(h, sid, at=first, agent_id=aid, agent_impl_name=aid,
                agent_impl_version=version)

        def checks():
            s = h.Factory()
            try:
                run_agent_checks(s, T1)
                s.commit()
                return [(e.id, e.status) for e in s.query(AISystemDriftEvent).filter_by(
                    ai_system_id=sid, drift_class="agent").order_by(AISystemDriftEvent.detected_at)]
            finally:
                s.close()

        release("2.0.0", _now() - timedelta(days=1))
        ((fid, _),) = checks()
        assert close(h, fid, "accepted_change", note="2.0 is the rollout").status_code == 200
        assert checks() == [(fid, "resolved")]
        release("3.0.0", _now() + timedelta(seconds=1))  # a NEW unapproved release
        rows = checks()
        assert len(rows) == 2 and rows[-1][1] == "active"


# ---------------------------------------------------------------------------
# Incident windows
# ---------------------------------------------------------------------------


def declare(h, *, sid=None, tenant_wide=False, hours=(10, 2), **extra):
    body = {
        "starts_at": (_now() - timedelta(hours=hours[0])).isoformat(),
        "ends_at": (_now() - timedelta(hours=hours[1])).isoformat(),
        "cause_category": "maintenance",
        "reason": "planned upgrade",
        **extra,
    }
    url = "/v1/admin/incident-windows" if tenant_wide else f"/v1/admin/ai-systems/{sid}/incident-windows"
    return h.client.post(url, json=body, headers=h.headers(T1))


class TestIncidentWindows:
    def test_declare_for_a_system_is_sealed_and_counts_its_runs(self, h, fake_core):
        sid = system(h)
        run(h, sid, at=_now() - timedelta(hours=5))
        run(h, sid, at=_now() - timedelta(hours=20))
        r = declare(h, sid=sid, resource_type="model", resource_id="gpt-x")
        assert r.status_code == 201, r.text
        w = r.json()
        assert (w["ai_system_id"], w["tenant_wide"], w["runs_excluded"]) == (sid, False, 1)
        assert w["seal"]["sealed"] and w["record_id"] == w["seal"]["record_id"]
        assert (w["resource_type"], w["resource_id"], w["declared_by"]) == ("model", "gpt-x", "admin_t1")
        rec = [x for x in fake_core.records() if x["context"].get("state") == "incident_window"
               and x["payload"]["incident_window"]["id"] == w["id"]]
        assert [x["payload"]["change"] for x in rec] == ["declared"]

    def test_the_system_list_includes_tenant_wide_windows(self, h):
        sid, other = system(h), system(h)
        own = declare(h, sid=sid).json()["id"]
        wide = declare(h, tenant_wide=True).json()["id"]
        theirs = declare(h, sid=other).json()["id"]
        ids = {w["id"] for w in h.client.get(f"/v1/admin/ai-systems/{sid}/incident-windows",
                                              headers=h.headers(T1)).json()["windows"]}
        assert {own, wide} <= ids and theirs not in ids
        tenant = {w["id"] for w in h.client.get("/v1/admin/incident-windows",
                                                 headers=h.headers(T1)).json()["windows"]}
        assert {own, wide, theirs} <= tenant

    def test_edit_is_a_new_sealed_declaration(self, h, fake_core):
        sid = system(h)
        w = declare(h, sid=sid).json()
        r = h.client.patch(f"/v1/admin/incident-windows/{w['id']}",
                           json={"cause_category": "provider_outage", "reason": "credits"},
                           headers=h.headers(T1))
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["cause_category"], body["reason"], body["updated_by"]) == (
            "provider_outage", "credits", "admin_t1")
        assert body["record_id"] and body["record_id"] != w["record_id"]
        changes = [x["payload"]["change"] for x in fake_core.records()
                   if x["context"].get("state") == "incident_window"
                   and x["payload"]["incident_window"]["id"] == w["id"]]
        assert changes == ["declared", "updated"]

    def test_bad_edits_are_422_and_change_nothing(self, h):
        sid = system(h)
        w = declare(h, sid=sid).json()
        bad = h.client.patch(f"/v1/admin/incident-windows/{w['id']}",
                             json={"ends_at": (_now() - timedelta(days=5)).isoformat()},
                             headers=h.headers(T1))
        assert bad.status_code == 422
        assert h.client.patch(f"/v1/admin/incident-windows/{w['id']}", json={},
                              headers=h.headers(T1)).status_code == 422
        s = h.Factory()
        try:
            assert _aware(s.get(AISystemIncidentWindow, w["id"]).ends_at) == _aware(
                datetime.fromisoformat(w["ends_at"]))
        finally:
            s.close()

    def test_withdraw_is_sealed_and_deletes(self, h, fake_core):
        sid = system(h)
        w = declare(h, sid=sid).json()
        r = h.client.delete(f"/v1/admin/incident-windows/{w['id']}", headers=h.headers(T1))
        assert r.status_code == 200 and r.json()["seal"]["sealed"]
        s = h.Factory()
        try:
            assert s.get(AISystemIncidentWindow, w["id"]) is None
        finally:
            s.close()
        assert any(x["payload"].get("change") == "withdrawn" for x in fake_core.records()
                   if x["context"].get("state") == "incident_window")

    @pytest.mark.parametrize("body", [
        {"cause_category": "bad_luck"},
        {"reason": ""},
        {"resource_type": "model"},  # without resource_id
        {"hours": (2, 10)},  # ends before it starts
    ])
    def test_invalid_declarations_are_422(self, h, body):
        sid = system(h)
        hours = body.pop("hours", (10, 2))
        assert declare(h, sid=sid, hours=hours, **body).status_code == 422

    def test_isolation(self, h):
        sid = system(h)
        w = declare(h, sid=sid).json()
        assert h.client.delete(f"/v1/admin/incident-windows/{w['id']}",
                               headers=h.headers("t2")).status_code == 404
        assert h.client.post(f"/v1/admin/ai-systems/{sid}/incident-windows", json={
            "starts_at": _now().isoformat(), "ends_at": (_now() + timedelta(hours=1)).isoformat(),
            "cause_category": "other", "reason": "x"}, headers=h.headers("t2")).status_code == 404
        assert w["id"] not in {x["id"] for x in h.client.get(
            "/v1/admin/incident-windows", headers=h.headers("t2")).json()["windows"]}

    def test_a_body_system_must_match_the_path(self, h):
        sid, other = system(h), system(h)
        assert declare(h, sid=sid, ai_system_id=other).status_code == 422


# ---------------------------------------------------------------------------
# Response policies honour the resolution (real evaluator + scheduler tick)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tick(h, monkeypatch):
    """The real closed-loop tick against the harness database."""
    from app.services.ai_system_response import scheduler

    monkeypatch.setattr(scheduler, "SessionLocal", h.Factory)
    return scheduler.run_once


def restore(h, sid, level="autonomous"):
    r = h.client.post(f"/v1/admin/ai-systems/{sid}/restore", json={"to_level": level},
                      headers=h.headers(T1))
    assert r.status_code == 200, r.text


def refusals(h, eid):
    return [r["code"] for r in (event_row(h, eid).response_taken or {}).get("refusals", [])]


class TestRestoreSticks:
    def test_the_demo_incident_does_not_re_demote(self, h, tick):
        """2026-09-28: resolved + restored at 06:45, re-fired from the SAME
        samples at 07:27 and demoted again. Now: resolved as accepted_change,
        the re-detection (same change point) is refused; only a new shift
        after the epoch fires."""
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        cp = _now() - timedelta(days=2)
        f1 = finding(h, sid, change_point=cp)
        tick()
        assert autonomy(h, sid) == "approval_required"

        assert close(h, f1, "accepted_change", note="the 09-26 retune").status_code == 200
        restore(h, sid)
        f2 = finding(h, sid, change_point=cp)  # the sweeper re-fires from the same data
        tick()
        assert autonomy(h, sid) == "autonomous"
        assert refusals(h, f2) == ["evidence_excluded"]

        f3 = finding(h, sid, change_point=_now() - timedelta(hours=1))  # new evidence
        tick()
        assert autonomy(h, sid) == "approval_required"
        assert not refusals(h, f3)

    def test_cause_fixed_refuses_evidence_inside_the_window(self, h, tick):
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        f1 = finding(h, sid, change_point=_now() - timedelta(hours=10))
        tick()
        close(h, f1, "cause_fixed", note="outage")
        restore(h, sid)
        f2 = finding(h, sid, metric="input_bytes", change_point=_now() - timedelta(hours=5))
        tick()
        assert autonomy(h, sid) == "autonomous"
        assert refusals(h, f2) == ["evidence_excluded"]

    def test_a_tenant_wide_window_covers_every_system(self, h, tick):
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        declare(h, tenant_wide=True, hours=(10, 1), cause_category="provider_outage")
        f = finding(h, sid, change_point=_now() - timedelta(hours=4))
        tick()
        assert autonomy(h, sid) == "autonomous"
        assert refusals(h, f) == ["evidence_excluded"]

    def test_a_window_never_excuses_a_fact_finding(self, h, tick):
        """An agent finding (unapproved release) inside an
        outage window is still a fact — the policy fires."""
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        declare(h, sid=sid, hours=(10, 1), cause_category="provider_outage")
        f = finding(h, sid, drift_class="agent", metric="agent.unapproved_release:a1",
                    change_point=_now() - timedelta(hours=4))
        tick()
        assert autonomy(h, sid) == "approval_required"
        assert not refusals(h, f)

    def test_false_positive_suppresses_the_same_evidence(self, h, tick):
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        det = _now() - timedelta(minutes=30)
        f1 = finding(h, sid, change_point=_now() - timedelta(hours=6), detected=det)
        close(h, f1, "false_positive", route="false-positive")
        f2 = finding(h, sid, change_point=_now() - timedelta(hours=6))
        tick()
        assert autonomy(h, sid) == "autonomous"
        assert refusals(h, f2) == ["evidence_excluded"]

    def test_an_epoch_on_another_metric_does_not_suppress(self, h, tick):
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        cp = _now() - timedelta(days=1)
        close(h, finding(h, sid, metric="action_count", change_point=cp), "accepted_change")
        f2 = finding(h, sid, metric="cost_per_run", change_point=cp)
        tick()
        assert autonomy(h, sid) == "approval_required"
        assert not refusals(h, f2)

    def test_a_finding_closed_before_the_tick_is_no_trigger(self, h, tick):
        sid = system(h, autonomy="autonomous")
        policy(h, sid)
        f = finding(h, sid, change_point=_now() - timedelta(hours=1))
        close(h, f, "cause_fixed", window_starts_at=(_now() - timedelta(hours=2)).isoformat(),
              window_ends_at=(_now() - timedelta(hours=1, minutes=30)).isoformat())
        tick()
        assert autonomy(h, sid) == "autonomous"
        assert event_row(h, f).response_taken is None  # never considered

    def test_an_envelope_breach_fixed_by_a_window_stays_fixed(self, h, tick):
        """The real assurance evaluator + emitter, then the real tick: after a
        cause_fixed window over the outage runs the next evaluation finds the
        term met, emits nothing, and the restore sticks."""
        from app.services.ai_system_assurance import upsert_health

        sid = system(h, autonomy="autonomous")
        policy(h, sid, drift_class="contract")
        closure = {"schema_version": 3, "ai_system": {"id": sid, "name": sid},
                   "composition": {"llm_models": []},
                   "operating_envelope": {"min_completion_rate": 0.9}}
        h.seed(lambda s: s.add(AISystemContract(
            id=f"ctr-{sid}", tenant_id=T1, ai_system_id=sid, version=1,
            contract_hash=hash_closure(closure), resolved_closure=closure, status="active",
            approved_by="approver")))
        for i in range(25):
            run(h, sid, at=_now() - timedelta(hours=30, minutes=i))
        # 25 completed, then 60 errored in the outage: 29 % completion, a
        # HIGH breach of the 90 % floor (the policy matches high and above).
        outage_start = _now() - timedelta(hours=12)
        for i in range(60):
            run(h, sid, at=outage_start + timedelta(minutes=10 * i), state="errored")

        def evaluate():
            s = h.Factory()
            try:
                upsert_health(s, s.get(ResourceGroup, sid))
                s.commit()
            finally:
                s.close()

        evaluate()
        s = h.Factory()
        try:
            (breach,) = s.query(AISystemDriftEvent).filter_by(
                ai_system_id=sid, drift_class="contract").all()
            bid = breach.id
        finally:
            s.close()
        tick()
        assert autonomy(h, sid) == "approval_required"

        r = close(h, bid, "cause_fixed", note="provider credits exhausted",
                  window_starts_at=(outage_start - timedelta(minutes=1)).isoformat(),
                  cause_category="provider_outage")
        assert r.status_code == 200, r.text
        restore(h, sid)
        evaluate()
        tick()
        assert autonomy(h, sid) == "autonomous"
        s = h.Factory()
        try:
            rows = s.query(AISystemDriftEvent).filter_by(ai_system_id=sid, drift_class="contract").all()
            assert [(e.id, e.resolution_kind) for e in rows] == [(bid, "cause_fixed")]
        finally:
            s.close()


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class TestReport:
    def test_windows_epochs_and_the_excluded_period(self, h):
        import jsonschema

        from app.services.ai_system_assurance import build_report
        from app.standards import REPORT_SPEC, REPORT_SPEC_VERSION, load_schema

        sid = system(h)
        for hours in (30, 20, 6, 5):
            run(h, sid, at=_now() - timedelta(hours=hours))
        w = declare(h, sid=sid, hours=(7, 4), cause_category="provider_outage").json()
        close(h, finding(h, sid, change_point=_now() - timedelta(hours=25)), "accepted_change")
        s = h.Factory()
        try:
            report = build_report(s, s.get(ResourceGroup, sid))
        finally:
            s.close()
        assert REPORT_SPEC_VERSION == "1.10"  # 1.10 judgement_exclusions.excluded_approval_requests
        ex = report["judgement_exclusions"]
        (win,) = ex["incident_windows"]
        assert (win["id"], win["runs_excluded"], win["declared_by"], win["reason"]) == (
            w["id"], 2, "admin_t1", "planned upgrade")
        assert [e["kind"] for e in ex["baseline_epochs"]] == ["accepted_change"]
        assert ex["excluded_runs"] == 2 and ex["excluded_hours"] == pytest.approx(3.0, abs=0.01)
        cov = report["coverage"]["excluded_period"]
        assert (cov["incident_windows"], cov["excluded_runs"]) == (1, 2)
        assert "unmeasured, not passed" in cov["statement"]
        assert any("unmeasured, not passed" in g for g in report["not_covered"])
        # Graded over judged runs, disclosed apart.
        runs = report["doing_its_job"]["runs"]
        assert (runs["total_runs"], runs["excluded_runs"]) == (2, 2)
        assert [e["resolution_kind"] for e in report["non_drifting"]["events"]] == ["accepted_change"]
        jsonschema.validate(report, load_schema(REPORT_SPEC))

    def test_no_window_says_every_run_was_judged(self, h):
        from app.services.ai_system_assurance import build_report

        sid = system(h)
        run(h, sid, at=_now() - timedelta(hours=3))
        s = h.Factory()
        try:
            report = build_report(s, s.get(ResourceGroup, sid))
        finally:
            s.close()
        assert report["judgement_exclusions"]["incident_windows"] == []
        assert report["coverage"]["excluded_period"]["statement"].startswith("No incident window")
        assert not any("unmeasured, not passed" in g for g in report["not_covered"])


# ---------------------------------------------------------------------------
# One decision, recorded once: the finding's Inbox item follows the close
# ---------------------------------------------------------------------------


def inbox_item(h, sid, *, metric="action_count", drift_class="behavioural", tenant=T1):
    """Raise the finding's Inbox item the way the closed loop does."""
    from app.services.assurance_inbox import AssuranceInboxService
    from app.services.drift_inbox_link import INBOX_CATEGORY_DRIFT, drift_inbox_fingerprint

    out = {}

    def _raise(s):
        item = AssuranceInboxService().raise_item(
            s, tenant_id=tenant, ai_system_id=sid, category=INBOX_CATEGORY_DRIFT,
            severity="warning", fingerprint=drift_inbox_fingerprint(sid, drift_class, metric),
            title=f"Drift detected: {drift_class} on {metric}", evidence={},
        )
        out["id"] = item.id

    h.seed(_raise)
    return out["id"]


def inbox_row(h, item_id):
    from app.database.models.assurance_inbox import AssuranceInboxEvent

    s = h.Factory()
    try:
        item = s.get(AssuranceInboxItem, item_id)
        events = (
            s.query(AssuranceInboxEvent).filter(AssuranceInboxEvent.item_id == item_id)
            .order_by(AssuranceInboxEvent.at).all()
        )
        s.expunge_all()
        return item, events
    finally:
        s.close()


class TestTheInboxItemFollowsTheClose:
    def test_every_human_kind_has_a_disposition(self):
        from app.constants.drift_resolution import HUMAN_RESOLUTION_KINDS
        from app.database.models.assurance_inbox import INBOX_DISPOSITIONS
        from app.services.drift_inbox_link import DISPOSITION_FOR_KIND

        assert set(DISPOSITION_FOR_KIND) == set(HUMAN_RESOLUTION_KINDS)
        assert set(DISPOSITION_FOR_KIND.values()) <= set(INBOX_DISPOSITIONS)

    @pytest.mark.parametrize("kind,route,disposition", [
        ("accepted_change", "resolve", "expected"),
        ("cause_fixed", "resolve", "fixed"),
        ("false_positive", "resolve", "false_positive"),
        ("false_positive", "false-positive", "false_positive"),
    ])
    def test_a_human_close_resolves_the_item_with_the_same_note_and_person(
        self, h, kind, route, disposition
    ):
        sid = system(h)
        eid = finding(h, sid)
        iid = inbox_item(h, sid)
        assert close(h, eid, kind, note="the retune lowered it", route=route).status_code == 200
        item, events = inbox_row(h, iid)
        assert (item.state, item.disposition, item.resolution_note) == (
            "resolved", disposition, "the retune lowered it")
        assert item.resolved_by == event_row(h, eid).resolved_by and item.resolved_by
        resolved = [e for e in events if e.event == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].detail == {
            "via": "drift_resolution", "drift_event_id": eid, "resolution_kind": kind,
            "disposition": disposition,
        }

    def test_the_item_stays_open_while_a_sibling_finding_is_open(self, h):
        sid = system(h)
        first, second = finding(h, sid), finding(h, sid)
        iid = inbox_item(h, sid)
        assert close(h, first, "accepted_change").status_code == 200
        assert inbox_row(h, iid)[0].state == "open"
        assert close(h, second, "accepted_change").status_code == 200
        assert inbox_row(h, iid)[0].state == "resolved"

    def test_another_metric_or_system_is_another_item(self, h):
        sid, other = system(h), system(h)
        eid = finding(h, sid, metric="cost_usd")
        same_system_other_metric = inbox_item(h, sid, metric="action_count")
        other_system_same_metric = inbox_item(h, other, metric="cost_usd")
        assert close(h, eid, "accepted_change").status_code == 200
        assert inbox_row(h, same_system_other_metric)[0].state == "open"
        assert inbox_row(h, other_system_same_metric)[0].state == "open"

    def test_a_close_without_an_item_is_still_a_close(self, h):
        sid = system(h)
        eid = finding(h, sid)
        assert close(h, eid, "cause_fixed").status_code == 200
        assert event_row(h, eid).resolution_kind == "cause_fixed"

    def test_auto_cleared_leaves_the_item_for_a_person(self, h):
        from app.services.drift_resolution import auto_clear

        sid = system(h)
        eid = finding(h, sid)
        iid = inbox_item(h, sid)

        def _clear(s):
            auto_clear(s.get(AISystemDriftEvent, eid), by="control-plane", note="cleared")

        h.seed(_clear)
        assert event_row(h, eid).resolution_kind == "auto_cleared"
        assert inbox_row(h, iid)[0].state == "open"

    @pytest.mark.parametrize("drift_class", ["contract", "agent"])
    def test_self_raised_classes_keep_their_own_items(self, h, drift_class):
        sid = system(h)
        eid = finding(h, sid, drift_class=drift_class)
        iid = inbox_item(h, sid, drift_class=drift_class)
        assert close(h, eid, "accepted_change").status_code == 200
        assert inbox_row(h, iid)[0].state == "open"

    def test_the_emitter_and_the_closer_share_one_fingerprint(self):
        """A drift fingerprint is built in one place; a literal anywhere else
        lets the emitter and the closer disagree, and the item never closes."""
        link = APP / "services" / "drift_inbox_link.py"
        found = [
            source_label(p) for p in python_sources(APP)
            if p != link and re.search(r"""f?["']drift:\{""", p.read_text())
        ]
        assert not found, f"build drift inbox fingerprints with drift_inbox_fingerprint(): {found}"
