# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the drift API.

The detection itself lives in gateway-core and is covered there. What is
worth defending on this side is what the operator console is allowed to
say and do:

  * **Tenant isolation.** Drift findings describe how a customer's systems
    behave. Leaking one across tenants is worse than leaking a config row.
  * **"Not drift" is not "resolved".** They are separate transitions
    because only the first is evidence the *detector* was wrong, and only
    the first may ever influence future thresholds. Collapsing them loses
    the one feedback signal that keeps the feature from being muted.
  * **Learning is not healthy.** The baselines endpoint has to report how
    many are still learning, or the UI shows an empty drift list for a
    system nothing is watching yet and calls it a clean bill of health.
  * **Unexplained findings are counted.** A high unexplained share is a
    fact about the detector, not the fleet, and the API surfaces it so the
    UI cannot quietly hide it.
  * Bad filter values are 422s, not silent empty lists.

Same harness approach as the other admin endpoint suites — in-memory
SQLite, FastAPI TestClient, seeded system admin + tenants.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.ai_system_drift import create_ai_system_drift_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.ai_system_drift import (
    AISystemBaseline,
    AISystemDriftEvent,
    BASELINE_ALL_ACTORS,
    BASELINE_LEARNING,
    BASELINE_READY,
    CAUSE_UNEXPLAINED,
    DRIFT_ACKNOWLEDGED,
    DRIFT_ACTIVE,
    DRIFT_BEHAVIOURAL,
    DRIFT_COST,
    DRIFT_FALSE_POSITIVE,
    DRIFT_RESOLVED,
)
from app.database.models.ai_system_drift_resolution import (
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
)
from app.database.models.assurance_inbox import AssuranceInboxEvent, AssuranceInboxItem
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    # tenants carries FKs to llm_model (default embedding/judge model).
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    ResourceGroup.__table__,
    AISystemBaseline.__table__,
    AISystemDriftEvent.__table__,
    # A close writes its effect (an epoch or a window).
    AISystemBaselineEpoch.__table__,
    AISystemIncidentWindow.__table__,
    # A close dispositions the finding's Inbox item.
    AssuranceInboxItem.__table__,
    AssuranceInboxEvent.__table__,
]


class _H:
    def __init__(self, client, Factory):
        self.client = client
        self.Factory = Factory

    def login(self, tenant_id: str) -> Dict[str, str]:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": f"admin_{tenant_id}",
                "password": "Secret123!",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def system_drift(self, tenant_id: str, system_id: str, **params):
        return self.client.get(
            f"/v1/admin/ai-systems/{system_id}/drift",
            headers=self.login(tenant_id),
            params=params,
        )

    def feed(self, tenant_id: str, **params):
        return self.client.get(
            "/v1/admin/drift", headers=self.login(tenant_id), params=params
        )

    def baselines(self, tenant_id: str, system_id: str):
        return self.client.get(
            f"/v1/admin/ai-systems/{system_id}/baselines",
            headers=self.login(tenant_id),
        )

    #: A close needs a kind and a note; ack takes neither.
    _DEFAULT_KIND = {"resolve": "accepted_change", "false-positive": "false_positive"}

    def triage(self, tenant_id: str, event_id: str, action: str, note=None):
        body = {"note": note} if note else {}
        if action in self._DEFAULT_KIND:
            body = {"kind": self._DEFAULT_KIND[action], "note": note or "triaged"}
        return self.client.post(
            f"/v1/admin/drift/{event_id}/{action}",
            headers=self.login(tenant_id),
            json=body,
        )


@pytest.fixture()
def harness(monkeypatch):
    # A close is sealed — never against a live core from a test.
    from tests.compliance_evidence_fakes import install

    install(monkeypatch)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        s = Factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test-ai-system-drift")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_ai_system_drift_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    s = Factory()
    try:
        for tid in ("t1", "t2"):
            s.add(
                Tenant(
                    id=tid, name=tid, display_name=tid.upper(),
                    organization_name=tid.upper(),
                    primary_contact_email=f"admin@{tid}.example",
                    is_active=True, is_default=False, is_trial=False,
                    suspended=False, billing_plan="self_hosted", features={},
                )
            )
            s.flush()
            role = TenantRole(
                id=f"role-{tid}-admin", tenant_id=tid, name="Tenant Admin",
                description="full-perm admin", is_system=True,
            )
            s.add(role)
            s.flush()
            for p in Permission:
                if p == Permission.SYSTEM_ADMIN:
                    continue
                s.add(
                    TenantRolePermission(
                        id=f"perm-{uuid.uuid4().hex[:10]}",
                        role_id=role.id, permission=p.value,
                    )
                )
            auth.create_tenant_admin(
                username=f"admin_{tid}", email=f"admin@{tid}.example",
                password="Secret123!", tenant_id=tid, db=s, role_id=role.id,
            )
        s.commit()
    finally:
        s.close()

    yield _H(client=client, Factory=Factory)
    app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _add_system(session, gid: str, tenant_id: str, kind: str = "agent"):
    session.add(
        ResourceGroup(
            id=gid, tenant_id=tenant_id, name=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind=kind,
        )
    )
    # Flushed here so the FK from drift events / baselines resolves within
    # the same transaction.
    session.flush()


def _add_event(
    session,
    eid: str,
    tenant_id: str,
    system_id: str,
    *,
    drift_class: str = DRIFT_BEHAVIOURAL,
    metric_key: str = "step_count",
    status: str = DRIFT_ACTIVE,
    cause: str = "model_change",
    hours_ago: int = 1,
    severity: str = "high",
):
    session.add(
        AISystemDriftEvent(
            id=eid,
            tenant_id=tenant_id,
            ai_system_id=system_id,
            drift_class=drift_class,
            metric_key=metric_key,
            detected_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            baseline_value=5.0,
            observed_value=14.0,
            effect_size=8.4,
            direction="up",
            severity=severity,
            attributed_cause=cause,
            cause_confidence=0.85,
            status=status,
            # A closed finding always carries its kind.
            resolution_kind=(
                None if status in (DRIFT_ACTIVE, DRIFT_ACKNOWLEDGED)
                else "false_positive" if status == DRIFT_FALSE_POSITIVE
                else "accepted_change"
            ),
        )
    )


def _add_baseline(
    session,
    bid: str,
    tenant_id: str,
    system_id: str,
    *,
    metric_key: str = "step_count",
    state: str = BASELINE_READY,
    sample_count: int = 120,
):
    session.add(
        AISystemBaseline(
            id=bid,
            tenant_id=tenant_id,
            ai_system_id=system_id,
            metric_key=metric_key,
            bucket="all",
            actor_id=BASELINE_ALL_ACTORS,
            median=5.0,
            mad=1.0,
            sample_count=sample_count,
            state=state,
        )
    )


def _seed(harness, fn):
    s = harness.Factory()
    try:
        fn(s)
        s.commit()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestPerSystemListing:
    def test_returns_findings_for_the_named_system_only(self, harness):
        def seed(s):
            _add_system(s, "sys-a", "t1")
            _add_system(s, "sys-b", "t1")
            _add_event(s, "d-a", "t1", "sys-a")
            _add_event(s, "d-b", "t1", "sys-b")

        _seed(harness, seed)

        r = harness.system_drift("t1", "sys-a")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [e["id"] for e in body["events"]] == ["d-a"]
        assert body["total"] == 1

    def test_newest_first(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "old", "t1", "sys", hours_ago=48)
            _add_event(s, "new", "t1", "sys", hours_ago=1)

        _seed(harness, seed)
        assert [e["id"] for e in harness.system_drift("t1", "sys").json()["events"]] == [
            "new",
            "old",
        ]

    def test_period_hours_bounds_the_window(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "recent", "t1", "sys", hours_ago=2)
            _add_event(s, "ancient", "t1", "sys", hours_ago=1000)

        _seed(harness, seed)
        ids = [
            e["id"]
            for e in harness.system_drift("t1", "sys", period_hours=24).json()["events"]
        ]
        assert ids == ["recent"]

    def test_period_hours_zero_means_everything(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "ancient", "t1", "sys", hours_ago=5000)

        _seed(harness, seed)
        body = harness.system_drift("t1", "sys", period_hours=0).json()
        assert body["total"] == 1


class TestTenantIsolation:
    def test_a_tenant_never_sees_another_tenants_findings(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_system(s, "sys-other", "t2")
            _add_event(s, "mine", "t1", "sys")
            _add_event(s, "theirs", "t2", "sys-other")

        _seed(harness, seed)
        assert [e["id"] for e in harness.feed("t1").json()["events"]] == ["mine"]
        assert [e["id"] for e in harness.feed("t2").json()["events"]] == ["theirs"]

    def test_guessing_an_id_from_another_tenant_is_a_404_not_a_transition(
        self, harness
    ):
        def seed(s):
            _add_system(s, "sys-other", "t2")
            _add_event(s, "theirs", "t2", "sys-other")

        _seed(harness, seed)
        assert harness.triage("t1", "theirs", "resolve").status_code == 404

        s = harness.Factory()
        try:
            assert (
                s.query(AISystemDriftEvent).filter_by(id="theirs").one().status
                == DRIFT_ACTIVE
            ), "a cross-tenant 404 must not have mutated the row on the way out"
        finally:
            s.close()

    def test_a_system_scoped_read_cannot_reach_across_tenants(self, harness):
        def seed(s):
            _add_system(s, "sys-other", "t2")
            _add_event(s, "theirs", "t2", "sys-other")

        _seed(harness, seed)
        # Correct system id, wrong tenant: an empty list, not the finding.
        assert harness.system_drift("t1", "sys-other").json()["total"] == 0


class TestTenantWideFeed:
    def test_defaults_to_open_findings(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "open", "t1", "sys", status=DRIFT_ACTIVE)
            _add_event(s, "done", "t1", "sys", status=DRIFT_RESOLVED)

        _seed(harness, seed)
        assert [e["id"] for e in harness.feed("t1").json()["events"]] == ["open"]

    def test_status_filter_can_ask_for_closed_ones(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "open", "t1", "sys", status=DRIFT_ACTIVE)
            _add_event(s, "done", "t1", "sys", status=DRIFT_RESOLVED)

        _seed(harness, seed)
        body = harness.feed("t1", status=DRIFT_RESOLVED).json()
        assert [e["id"] for e in body["events"]] == ["done"]

    def test_drift_class_filter(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "b", "t1", "sys", drift_class=DRIFT_BEHAVIOURAL)
            _add_event(s, "c", "t1", "sys", drift_class=DRIFT_COST)

        _seed(harness, seed)
        body = harness.feed("t1", drift_class=DRIFT_COST).json()
        assert [e["id"] for e in body["events"]] == ["c"]

    def test_limit_caps_the_feed(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            for i in range(5):
                _add_event(s, f"d{i}", "t1", "sys", hours_ago=i + 1)

        _seed(harness, seed)
        assert len(harness.feed("t1", limit=2).json()["events"]) == 2


class TestBadFiltersAreRejected:
    def test_unknown_status_is_422(self, harness):
        r = harness.feed("t1", status="probably_fine")
        assert r.status_code == 422
        assert "status must be one of" in r.text

    def test_unknown_drift_class_is_422(self, harness):
        r = harness.feed("t1", drift_class="vibes")
        assert r.status_code == 422
        assert "drift_class must be one of" in r.text

    def test_a_bad_filter_is_never_an_empty_list(self, harness):
        # An empty list reads as "no drift", which is the opposite of
        # "your query was wrong". This is the whole point of validating.
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "d1", "t1", "sys")

        _seed(harness, seed)
        assert harness.feed("t1", status="nonsense").status_code == 422


class TestUnexplainedCount:
    def test_counts_open_findings_with_no_identified_cause(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "explained", "t1", "sys", cause="model_change")
            _add_event(s, "unexplained", "t1", "sys", cause=CAUSE_UNEXPLAINED)

        _seed(harness, seed)
        body = harness.system_drift("t1", "sys").json()
        assert body["open_count"] == 2
        assert body["unexplained_count"] == 1

    def test_closed_unexplained_findings_do_not_inflate_the_count(self, harness):
        # The number is meant to say "how much of what is on your plate has
        # no explanation", not "how often has the ladder ever failed".
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(
                s, "closed", "t1", "sys",
                cause=CAUSE_UNEXPLAINED, status=DRIFT_RESOLVED,
            )

        _seed(harness, seed)
        body = harness.system_drift("t1", "sys").json()
        assert body["unexplained_count"] == 0
        assert body["open_count"] == 0


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


class TestTriage:
    def _one_active(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "d1", "t1", "sys")

        _seed(harness, seed)

    def test_ack_marks_it_acknowledged_without_closing_it(self, harness):
        self._one_active(harness)
        r = harness.triage("t1", "d1", "ack")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == DRIFT_ACKNOWLEDGED
        assert body["is_open"] is True, (
            "acknowledging is 'I am looking at this', not 'this is dealt with' — "
            "closing it here would drop the finding off the operator's list"
        )
        assert body["resolved_at"] is None

    def test_resolve_closes_it_and_records_who(self, harness):
        self._one_active(harness)
        body = harness.triage("t1", "d1", "resolve").json()
        assert body["status"] == DRIFT_RESOLVED
        assert body["is_open"] is False
        assert body["resolved_by"] == "admin_t1"
        assert body["resolved_at"] is not None

    def test_false_positive_is_a_distinct_status_not_a_flavour_of_resolved(
        self, harness
    ):
        # These must stay separate: only false-positive is evidence the
        # *detector* was wrong, and only it may ever feed back into future
        # thresholds. Collapsing them destroys that signal, and a detector
        # that cannot be told it was wrong gets muted within a month.
        self._one_active(harness)
        body = harness.triage("t1", "d1", "false-positive").json()
        assert body["status"] == DRIFT_FALSE_POSITIVE
        assert body["status"] != DRIFT_RESOLVED
        assert body["is_open"] is False

    def test_a_note_is_stored_with_the_transition(self, harness):
        self._one_active(harness)
        body = harness.triage("t1", "d1", "resolve", note="rolled the model back").json()
        assert body["resolution_note"] == "rolled the model back"

    def test_transitions_persist(self, harness):
        self._one_active(harness)
        harness.triage("t1", "d1", "false-positive")
        s = harness.Factory()
        try:
            assert (
                s.query(AISystemDriftEvent).filter_by(id="d1").one().status
                == DRIFT_FALSE_POSITIVE
            )
        finally:
            s.close()

    def test_an_unknown_event_is_404(self, harness):
        assert harness.triage("t1", "no-such-event", "ack").status_code == 404


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


class TestBaselines:
    def test_lists_baselines_for_the_system(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_baseline(s, "b1", "t1", "sys", metric_key="step_count")
            _add_baseline(s, "b2", "t1", "sys", metric_key="cost_per_run")

        _seed(harness, seed)
        body = harness.baselines("t1", "sys").json()
        assert body["total"] == 2
        assert [b["metric_key"] for b in body["baselines"]] == [
            "cost_per_run",
            "step_count",
        ]

    def test_reports_how_many_are_still_learning(self, harness):
        # Without this the UI cannot distinguish "no drift" from "nothing is
        # being watched yet", and shows an empty list as a clean bill of
        # health for a system that has no baseline at all.
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_baseline(s, "b1", "t1", "sys", state=BASELINE_READY)
            _add_baseline(
                s, "b2", "t1", "sys", metric_key="cost_per_run",
                state=BASELINE_LEARNING, sample_count=8,
            )

        _seed(harness, seed)
        body = harness.baselines("t1", "sys").json()
        assert body["learning"] == 1
        assert body["total"] == 2

    def test_is_ready_is_exposed_per_baseline(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_baseline(
                s, "b1", "t1", "sys", state=BASELINE_LEARNING, sample_count=8
            )

        _seed(harness, seed)
        b = harness.baselines("t1", "sys").json()["baselines"][0]
        assert b["is_ready"] is False
        assert b["sample_count"] == 8

    def test_baselines_are_tenant_scoped(self, harness):
        def seed(s):
            _add_system(s, "shared-id", "t2")
            _add_baseline(s, "b-other", "t2", "shared-id")

        _seed(harness, seed)
        assert harness.baselines("t1", "shared-id").json()["total"] == 0


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_the_cause_travels_with_the_finding(self, harness):
        # The whole point of the phase: an alert that arrives without its
        # explanation hands the investigation back to the person reading it.
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "d1", "t1", "sys", cause="model_change")

        _seed(harness, seed)
        e = harness.system_drift("t1", "sys").json()["events"][0]
        assert e["attributed_cause"] == "model_change"
        assert e["cause_confidence"] == 0.85
        assert e["is_explained"] is True

    def test_an_unexplained_finding_says_so_rather_than_omitting_the_field(
        self, harness
    ):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "d1", "t1", "sys", cause=CAUSE_UNEXPLAINED)

        _seed(harness, seed)
        e = harness.system_drift("t1", "sys").json()["events"][0]
        assert e["attributed_cause"] == CAUSE_UNEXPLAINED
        assert e["is_explained"] is False

    def test_the_numbers_behind_the_finding_are_present(self, harness):
        def seed(s):
            _add_system(s, "sys", "t1")
            _add_event(s, "d1", "t1", "sys")

        _seed(harness, seed)
        e = harness.system_drift("t1", "sys").json()["events"][0]
        for field in (
            "baseline_value",
            "observed_value",
            "effect_size",
            "direction",
            "severity",
            "drift_class",
            "metric_key",
            "detected_at",
        ):
            assert e.get(field) is not None, f"{field} missing from the response"
