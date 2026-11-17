# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Incident register — deadlines, sealing, inbox

Deadlines are computed per framework from the catalogs' `deadline_rules`
and the incident's classification (EU 15 / 10 / 2 days; GDPR 72 h; HIPAA
60 days; DORA and NIS2 staged, each stage on its own anchor — see
`test_compliance_incident_stages.py`), shown and never enforced. Every state change is an event row and a sealed
`ai.brutor.incident` record; free text enters only as a payload digest. A
core that cannot seal never loses the change.
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
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.compliance_incident import ComplianceIncident
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import incidents as svc
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness

AWARE = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
BASE = "/v1/admin/compliance/incidents"


def _due(deadlines, fid):
    return {k: datetime.fromisoformat(v) - AWARE for k, v in deadlines[fid].items()}


class TestDeadlineComputation:
    def test_eu_alternatives_follow_the_classification(self):
        assert _due(svc.compute_deadlines(["eu-ai-act"], "serious_incident", AWARE), "eu-ai-act") == {
            "standard": timedelta(days=15)
        }
        assert _due(svc.compute_deadlines(["eu-ai-act"], "death", AWARE), "eu-ai-act") == {"death": timedelta(days=10)}
        assert _due(svc.compute_deadlines(["eu-ai-act"], "widespread", AWARE), "eu-ai-act") == {
            "widespread": timedelta(days=2)
        }

    def test_gdpr_and_hipaa(self):
        d = svc.compute_deadlines(["gdpr", "hipaa"], "breach", AWARE)
        assert _due(d, "gdpr") == {"standard": timedelta(hours=72)}
        assert _due(d, "hipaa") == {"standard": timedelta(days=60)}

    def test_dora_and_nis2_store_the_stages_whose_clock_started(self):
        # Not every stage is measured from awareness (4 h / 72 h / 30 days
        # would be wrong in law). Unclassified, unsubmitted: DORA's initial notification is
        # bounded at 24 h from awareness and the later stages wait for a
        # submission; NIS2's first two stages run from awareness.
        # Full coverage: tests/test_compliance_incident_stages.py.
        d = svc.compute_deadlines(["dora", "nis2"], "major_incident", AWARE)
        assert _due(d, "dora") == {"initial": timedelta(hours=24)}
        assert _due(d, "nis2") == {"early_warning": timedelta(hours=24), "notification": timedelta(hours=72)}

    def test_framework_without_rules_has_no_clock(self):
        assert svc.compute_deadlines(["soc2", "iso-42001"], "x", AWARE) == {"soc2": {}, "iso-42001": {}}

    def test_status_first_stage_is_the_report_later_stages_wait(self):
        # Later DORA stages are not "open" from awareness, nor "closed"
        # (satisfied) by closing the incident: they wait for the previous stage's submission, and
        # closing without submitting them is `closed_unsubmitted`.
        inc = ComplianceIncident(
            id="i", tenant_id="t", classification="major_incident", severity="high", title="t",
            became_aware_at=AWARE, frameworks=["dora"], status="open", stage_submissions={},
            deadlines=svc.compute_deadlines(["dora"], "major_incident", AWARE),
        )
        rows = svc.deadline_status(inc, now=AWARE + timedelta(hours=3, minutes=30))
        assert [(r["rule"], r["stage"], r["status"]) for r in rows] == [
            ("initial", "report", "due_soon"), ("intermediate", "follow_up", "waiting"),
            ("final", "follow_up", "waiting"),
        ]
        rows = svc.deadline_status(inc, now=AWARE + timedelta(hours=25))
        assert rows[0]["status"] == "overdue"
        inc.reported_at = AWARE + timedelta(hours=26)
        inc.status = "reported"
        rows = svc.deadline_status(inc, now=AWARE + timedelta(hours=27))
        assert rows[0]["status"] == "reported_late"
        inc.status = "closed"
        assert {r["status"] for r in svc.deadline_status(inc, now=AWARE + timedelta(days=40))[1:]} == {
            "closed_unsubmitted"
        }


def seed(session):
    for gid, tenant in (("sys-a", "t1"), ("sys-b", "t2")):
        session.add(ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
        ))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_compliance_incidents_router(BrutorLogger("incident-tests"))])
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def core(monkeypatch):
    return fakes.install(monkeypatch)


def _create(harness, tenant="t1", **over):
    body = {
        "title": "Credit model denied a protected group",
        "description": "Free text that must never enter a record.",
        "classification": "serious_incident",
        "severity": "high",
        "became_aware_at": AWARE.isoformat(),
        "frameworks": ["eu-ai-act", "gdpr", "dora"],
        "ai_system_id": "sys-a",
        **over,
    }
    r = harness.client.post(BASE, json=body, headers=harness.headers(tenant))
    return r


class TestIncidentLifecycle:
    def test_create_computes_deadlines_and_seals(self, harness, core):
        r = _create(harness)
        assert r.status_code == 201, r.text
        body = r.json()
        inc = body["incident"]
        assert inc["deadlines_enforced"] is False
        assert set(inc["deadlines"]) == {"eu-ai-act", "gdpr", "dora"}
        assert {(d["framework_id"], d["rule"]) for d in inc["deadline_status"]} >= {
            ("eu-ai-act", "standard"), ("gdpr", "standard"), ("dora", "initial"), ("dora", "final"),
        }
        assert body["seal"]["sealed"] is True
        assert inc["last_record_id"] == body["seal"]["record_id"] == body["event"]["record_id"]
        (rec,) = core.records("ai.brutor.incident")
        assert rec["context"]["state"] == "created"
        assert rec["context"]["incident_id"] == inc["id"]
        assert rec["context"]["tags"] == ["dora", "eu-ai-act", "gdpr"]
        assert rec["ai_system_id"] == "sys-a"
        # Free text only in the payload (digested by the core, never stored).
        assert "Free text" in rec["payload"]["description"]
        assert "Free text" not in str({k: v for k, v in rec.items() if k != "payload"})

    def test_every_state_change_is_sealed_and_chained(self, harness, core):
        inc = _create(harness).json()["incident"]
        iid = inc["id"]
        r = harness.client.patch(f"{BASE}/{iid}", json={"classification": "death"}, headers=harness.headers())
        assert r.status_code == 200, r.text
        assert r.json()["incident"]["deadlines"]["eu-ai-act"].keys() == {"death"}
        assert r.json()["event"]["detail"] == {"changed": {"classification": "death"}}
        r = harness.client.post(f"{BASE}/{iid}/events", json={"note": "Called the regulator"}, headers=harness.headers())
        assert r.status_code == 201
        r = harness.client.post(
            f"{BASE}/{iid}/report",
            json={"authority": "Market surveillance authority", "reported_at": (AWARE + timedelta(days=3)).isoformat(),
                  "external_reference": "MSA-1"},
            headers=harness.headers(),
        )
        assert r.status_code == 200, r.text
        reported = r.json()["incident"]
        assert reported["status"] == "reported"
        eu = next(d for d in reported["deadline_status"] if d["framework_id"] == "eu-ai-act")
        assert eu["status"] == "reported_on_time"
        dora = next(d for d in reported["deadline_status"] if d["rule"] == "initial")
        assert dora["status"] == "reported_late"
        r = harness.client.post(f"{BASE}/{iid}/close", json={"note": "done"}, headers=harness.headers())
        assert r.status_code == 200

        records = [r for r in core.records("ai.brutor.incident") if r["context"]["incident_id"] == iid]
        assert [r["context"]["state"] for r in records] == ["created", "updated", "note", "reported", "closed"]
        # Each record after the first references the one before it.
        for prev, rec in zip(records, records[1:]):
            refs = rec.get("references") or []
            assert [x["purpose"] for x in refs] == ["previous_state"]

        events = harness.client.get(f"{BASE}/{iid}/events", headers=harness.headers()).json()
        assert [e["event"] for e in events["items"]] == ["created", "updated", "note", "reported", "closed"]
        assert all(e["record_id"] for e in events["items"])
        detail = harness.client.get(f"{BASE}/{iid}", headers=harness.headers()).json()
        assert detail["status"] == "closed" and len(detail["events"]) == 5

        # Closed is final.
        r = harness.client.patch(f"{BASE}/{iid}", json={"severity": "low"}, headers=harness.headers())
        assert r.status_code == 409

    def test_noop_patch_writes_nothing(self, harness, core):
        inc = _create(harness).json()["incident"]
        before = len(core.seal_calls)
        r = harness.client.patch(f"{BASE}/{inc['id']}", json={"severity": "high"}, headers=harness.headers())
        assert r.status_code == 200 and r.json()["event"] is None
        assert len(core.seal_calls) == before

    @pytest.mark.parametrize("mode", [fakes.MODE_DISABLED, fakes.MODE_UNREACHABLE])
    def test_unsealed_change_is_saved_and_says_so(self, harness, core, mode):
        core.mode = mode
        r = _create(harness)
        assert r.status_code == 201
        body = r.json()
        assert body["seal"] == {"sealed": False, "record_id": None, "seal_error": mode,
                                "seal_detail": f"fake core: {mode}"}
        assert body["incident"]["last_record_id"] is None
        assert body["event"]["record_id"] is None

    def test_validation(self, harness):
        assert _create(harness, frameworks=["nope"]).status_code == 422
        assert _create(harness, frameworks=[]).status_code == 422
        assert _create(harness, severity="apocalyptic").status_code == 422
        assert _create(harness, ai_system_id="sys-b").status_code == 404  # another tenant's system
        assert _create(harness, linked_run_ids=["run-missing"]).status_code == 422
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        assert _create(harness, became_aware_at=future).status_code == 422

    def test_list_filters_and_isolation(self, harness):
        _create(harness, frameworks=["hipaa"], classification="breach", ai_system_id=None)
        items = harness.client.get(BASE, params={"framework": "hipaa"}, headers=harness.headers()).json()["items"]
        assert items and all("hipaa" in i["frameworks"] for i in items)
        other = harness.client.get(BASE, headers=harness.headers("t2")).json()
        assert other["total"] == 0
        some_id = items[0]["id"]
        assert harness.client.get(f"{BASE}/{some_id}", headers=harness.headers("t2")).status_code == 404


class TestDeadlineInbox:
    def test_due_soon_and_overdue_raise_one_item_each(self, harness):
        s = harness.Factory()
        try:
            now = datetime.now(timezone.utc)
            change = svc.create_incident(
                s, "t1", title="GDPR breach", description="", classification="breach", severity="high",
                became_aware_at=now - timedelta(hours=60), frameworks=["gdpr", "eu-ai-act"], actor="t",
            )
            s.commit()
            iid = change.incident.id
            assert svc.scan_deadlines(s, now=now) >= 1
            assert svc.scan_deadlines(s, now=now) >= 1  # re-run bumps, never duplicates
            s.commit()
            items = s.query(AssuranceInboxItem).filter(
                AssuranceInboxItem.category == "incident_deadline",
                AssuranceInboxItem.fingerprint.like(f"incident_deadline:{iid}:%"),
            ).all()
            assert [i.fingerprint for i in items] == [f"incident_deadline:{iid}:gdpr:standard"]
            assert items[0].severity == "warning" and items[0].occurrence_count == 2
            assert items[0].id in s.get(ComplianceIncident, iid).linked_inbox_item_ids

            later = now + timedelta(hours=13)
            svc.scan_deadlines(s, now=later)
            s.commit()
            item = s.get(AssuranceInboxItem, items[0].id)
            assert item.severity == "critical"  # passed ⇒ escalated on the same open item

            svc.report_incident(s, s.get(ComplianceIncident, iid), authority="DPA", reported_at=later)
            s.commit()
            before = s.get(AssuranceInboxItem, items[0].id).occurrence_count
            svc.scan_deadlines(s, now=later + timedelta(minutes=1))
            s.commit()
            assert s.get(AssuranceInboxItem, items[0].id).occurrence_count == before
        finally:
            s.close()


class TestResolverReadsStoredDeadlines:
    """`resolvers.documents.incident_deadline` reads the `{framework: {rule:
    ISO}}` map `compute_deadlines` stores, instead of recomputing from
    today's catalog (which may have changed since the incident was raised)."""

    AWARE = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)

    def _deadline(self, deadlines, classification="standard"):
        from app.services.compliance.resolvers.documents import incident_deadline

        return incident_deadline(
            {"classification": classification, "became_aware_at": self.AWARE, "deadlines": deadlines},
            "eu-ai-act",
        )

    def test_stored_map_matches_compute_deadlines(self):
        stored = svc.compute_deadlines(["eu-ai-act"], "standard", self.AWARE)
        assert stored["eu-ai-act"], "eu-ai-act has a statutory clock"
        earliest = min(datetime.fromisoformat(v) for v in stored["eu-ai-act"].values())
        assert self._deadline(stored) == earliest

    def test_stored_deadline_differing_from_the_catalog_wins(self):
        # Catalog default for `standard` today is 15 days; this incident was
        # raised under a catalog that allowed 30.
        catalog_due = self._deadline({})
        assert catalog_due == self.AWARE + timedelta(days=15)
        frozen = (self.AWARE + timedelta(days=30)).isoformat()
        assert self._deadline({"eu-ai-act": {"standard": frozen}}) == self.AWARE + timedelta(days=30)

    def test_earliest_stage_is_the_report_deadline(self):
        stages = {"final": (self.AWARE + timedelta(days=30)).isoformat(),
                  "initial": (self.AWARE + timedelta(days=2)).isoformat()}
        assert self._deadline({"eu-ai-act": stages}) == self.AWARE + timedelta(days=2)

    def test_stored_empty_entry_means_no_clock(self):
        assert self._deadline({"eu-ai-act": {}}) is None

    def test_legacy_shapes_still_read(self):
        due = self.AWARE + timedelta(days=3)
        assert self._deadline({"eu-ai-act": due.isoformat()}) == due
        assert self._deadline({"eu-ai-act": {"due_at": due.isoformat()}}) == due
        assert self._deadline({"eu-ai-act": [due.isoformat(), (due + timedelta(days=1)).isoformat()]}) == due
