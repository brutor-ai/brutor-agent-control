# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance Inbox.

What is worth defending:

  * **Dedup is the storm-shield.** A recurring signal must bump the ONE
    open item (occurrence_count, last_seen, severity max), never raise a
    second row — and a resolved-then-reoccurring fingerprint must start a
    FRESH item linked to the prior one, so "we keep resolving this" is
    visible rather than laundered into a counter.
  * **Dispositions are evidence.** Resolving requires a disposition AND a
    note; every transition writes an append-only event; reopening never
    erases the resolved event from the trail.
  * **Review debt reaches health honestly.** No items ever → None
    (unmeasured is not a free pass and not a failure). Items but no
    overdue criticals → 1.0. An open critical past the SLA → 0.5, which
    the worst-of scorer turns into `degraded`.
  * **Tenant isolation** on every route, and bad enum filters are 422s.

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
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.assurance_inbox import create_assurance_inbox_router
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
from app.database.models.assurance_inbox import (
    AssuranceInboxEvent,
    AssuranceInboxItem,
)
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services.assurance_inbox import AssuranceInboxService

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
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

    def list(self, tenant_id: str, **params):
        return self.client.get(
            "/v1/admin/assurance-inbox", headers=self.login(tenant_id), params=params
        )

    def counts(self, tenant_id: str):
        return self.client.get(
            "/v1/admin/assurance-inbox/counts", headers=self.login(tenant_id)
        )

    def detail(self, tenant_id: str, item_id: str):
        return self.client.get(
            f"/v1/admin/assurance-inbox/{item_id}", headers=self.login(tenant_id)
        )

    def action(self, tenant_id: str, item_id: str, action: str, json=None):
        return self.client.post(
            f"/v1/admin/assurance-inbox/{item_id}/{action}",
            headers=self.login(tenant_id),
            json=json if json is not None else {},
        )


@pytest.fixture()
def harness():
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
    logger = BrutorLogger("test-assurance-inbox")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_assurance_inbox_router(logger), prefix="/v1")
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


def _add_system(session, gid: str, tenant_id: str):
    session.add(
        ResourceGroup(
            id=gid, tenant_id=tenant_id, name=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind="agent",
        )
    )
    session.flush()


def _raise(session, service, tenant_id="t1", **over) -> AssuranceInboxItem:
    kwargs = dict(
        tenant_id=tenant_id,
        ai_system_id="sys-1",
        category="drift",
        severity="warning",
        fingerprint="drift:sys-1:behavioural:step_count",
        title="Drift detected: behavioural on step_count",
        evidence={"drift_event_id": "drift-1"},
    )
    kwargs.update(over)
    item = service.raise_item(session, **kwargs)
    session.commit()
    assert item is not None
    return item


# ═══════════════════════ service: dedup + trail ═════════════════════════


class TestDedup:
    def test_reoccurrence_bumps_the_open_item_not_a_second_row(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        first = _raise(s, svc)
        second = _raise(s, svc)
        assert second.id == first.id
        assert second.occurrence_count == 2
        assert s.query(AssuranceInboxItem).count() == 1
        s.close()

    def test_severity_only_escalates_never_downgrades(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        _raise(s, svc, severity="warning")
        item = _raise(s, svc, severity="critical")
        assert item.severity == "critical"
        item = _raise(s, svc, severity="info")
        assert item.severity == "critical"
        s.close()

    def test_resolved_then_reoccurring_starts_a_fresh_linked_item(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        first = _raise(s, svc)
        svc.resolve(s, first, admin="op", disposition="expected", note="known load test")
        s.commit()
        fresh = _raise(s, svc)
        assert fresh.id != first.id
        assert fresh.occurrence_count == 1
        created = (
            s.query(AssuranceInboxEvent)
            .filter(
                AssuranceInboxEvent.item_id == fresh.id,
                AssuranceInboxEvent.event == "created",
            )
            .one()
        )
        assert created.detail == {"previous_item_id": first.id}
        s.close()

    def test_raise_never_raises_even_on_garbage(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        assert (
            svc.raise_item(
                s, tenant_id="t1", category="not_a_category", severity="warning",
                fingerprint="x", title="x",
            )
            is None
        )
        s.close()


# ═══════════════════════ service: state machine ═════════════════════════


class TestStateMachine:
    def test_resolve_requires_disposition_and_note(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc)
        with pytest.raises(ValueError):
            svc.resolve(s, item, admin="op", disposition="whatever", note="n")
        with pytest.raises(ValueError):
            svc.resolve(s, item, admin="op", disposition="fixed", note="   ")
        s.close()

    def test_reopen_keeps_the_resolved_event_in_the_trail(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc)
        svc.resolve(s, item, admin="op", disposition="false_positive", note="tuning")
        svc.reopen(s, item, admin="op2", reason="it is real after all")
        s.commit()
        events = [
            e.event
            for e in s.query(AssuranceInboxEvent)
            .filter(AssuranceInboxEvent.item_id == item.id)
            .order_by(AssuranceInboxEvent.at.asc(), AssuranceInboxEvent.id.asc())
            .all()
        ]
        assert events == ["created", "resolved", "reopened"]
        assert item.state == "open"
        assert item.disposition is None
        s.close()


# ═══════════════════════ service: review debt → health ══════════════════


class TestReviewScore:
    def test_no_items_ever_is_unmeasured_not_a_free_pass(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        assert svc.review_score(db=s, tenant_id="t1", ai_system_id="sys-1") is None
        s.close()

    def test_no_overdue_criticals_scores_full(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        _raise(s, svc, severity="critical")  # fresh — inside SLA
        assert svc.review_score(db=s, tenant_id="t1", ai_system_id="sys-1") == 1.0
        s.close()

    def test_open_critical_past_sla_degrades(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc, severity="critical")
        item.first_seen_at = datetime.now(timezone.utc) - timedelta(days=10)
        s.commit()
        assert svc.review_score(db=s, tenant_id="t1", ai_system_id="sys-1") == 0.5
        # Resolving the debt restores the score.
        svc.resolve(s, item, admin="op", disposition="fixed", note="patched")
        s.commit()
        assert svc.review_score(db=s, tenant_id="t1", ai_system_id="sys-1") == 1.0
        s.close()

    def test_worst_of_health_turns_review_debt_into_degraded(self):
        from app.database.models.ai_system_health import score_health

        scored = score_health(reliability=1.0, cost=1.0, review=0.5)
        assert scored["health_state"] == "degraded"
        assert scored["review_score"] == 0.5


# ═══════════════════════ endpoints ══════════════════════════════════════


class TestEndpoints:
    def test_list_filters_and_isolation(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        _raise(s, svc, fingerprint="f1", category="drift", severity="warning")
        _raise(s, svc, fingerprint="f2", category="guardrail_block", severity="critical")
        _raise(s, svc, tenant_id="t2", fingerprint="f3")
        s.close()

        r = harness.list("t1")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 2

        r = harness.list("t1", category="guardrail_block")
        assert [i["fingerprint"] for i in r.json()["items"]] == ["f2"]

        # The other tenant sees only its own item.
        r = harness.list("t2")
        assert [i["fingerprint"] for i in r.json()["items"]] == ["f3"]

        # Bad enum values are 422s, not silent empty lists.
        assert harness.list("t1", state="weird").status_code == 422
        assert harness.list("t1", severity="weird").status_code == 422
        assert harness.list("t1", category="weird").status_code == 422

    def test_counts_track_open_critical(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        _raise(s, svc, fingerprint="f1", severity="critical")
        _raise(s, svc, fingerprint="f2", severity="info")
        _raise(s, svc, fingerprint="f3", severity="warning")
        _raise(s, svc, fingerprint="f4", severity="warning")
        s.close()
        r = harness.counts("t1")
        assert r.status_code == 200
        body = r.json()
        assert body["open"] == 4
        assert body["open_critical"] == 1
        # The sidebar badges: critical and warning, each on its own.
        assert body["open_warning"] == 2
        assert body["open_info"] == 1

    def test_full_triage_flow_writes_audit_and_trail(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc)
        item_id = item.id
        s.close()

        r = harness.action("t1", item_id, "acknowledge")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "acknowledged"

        r = harness.action("t1", item_id, "assign", json={"assignee": "carol"})
        assert r.status_code == 200
        assert r.json()["assignee_admin"] == "carol"

        # Resolve without a note is rejected by schema (min_length).
        r = harness.action(
            "t1", item_id, "resolve", json={"disposition": "fixed", "note": ""}
        )
        assert r.status_code == 422

        r = harness.action(
            "t1", item_id, "resolve",
            json={"disposition": "true_positive", "note": "guardrail was right"},
        )
        assert r.status_code == 200
        assert r.json()["disposition"] == "true_positive"

        r = harness.action("t1", item_id, "reopen", json={"reason": "second look"})
        assert r.status_code == 200
        assert r.json()["state"] == "open"

        # Trail carries the whole story.
        r = harness.detail("t1", item_id)
        events = [e["event"] for e in r.json()["events"]]
        assert events == [
            "created", "acknowledged", "assigned", "resolved", "reopened",
        ]

        # Every mutation was admin-audited.
        s = harness.Factory()
        actions = [
            row.action
            for row in s.query(TenantAdminAuditLog)
            .filter(TenantAdminAuditLog.action.like("assurance_inbox.%"))
            .all()
        ]
        s.close()
        assert set(actions) == {
            "assurance_inbox.acknowledge",
            "assurance_inbox.assign",
            "assurance_inbox.resolve",
            "assurance_inbox.reopen",
        }

    def test_cross_tenant_item_access_is_404(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc)
        item_id = item.id
        s.close()
        assert harness.detail("t2", item_id).status_code == 404
        assert (
            harness.action(
                "t2", item_id, "resolve",
                json={"disposition": "fixed", "note": "x"},
            ).status_code
            == 404
        )

    def test_bulk_acknowledge_only_touches_open_own_tenant_items(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        a = _raise(s, svc, fingerprint="f1")
        b = _raise(s, svc, fingerprint="f2")
        other = _raise(s, svc, tenant_id="t2", fingerprint="f3")
        ids = [a.id, b.id, other.id, "inbox-nonexistent"]
        s.close()

        r = harness.client.post(
            "/v1/admin/assurance-inbox/bulk-acknowledge",
            headers=harness.login("t1"),
            json={"item_ids": ids},
        )
        assert r.status_code == 200, r.text
        assert r.json()["acknowledged"] == 2
        s = harness.Factory()
        assert (
            s.query(AssuranceInboxItem)
            .filter(AssuranceInboxItem.id == other.id)
            .one()
            .state
            == "open"
        )
        s.close()

    def test_human_review_section_shapes_the_report_block(self, harness):
        svc = AssuranceInboxService()
        s = harness.Factory()
        _add_system(s, "sys-1", "t1")
        item = _raise(s, svc, fingerprint="f1", severity="critical")
        _raise(s, svc, fingerprint="f2", severity="warning")
        svc.resolve(s, item, admin="op", disposition="true_positive", note="real")
        s.commit()
        section = svc.human_review_section(
            s, tenant_id="t1", ai_system_id="sys-1", period_hours=24
        )
        s.close()
        assert section["raised"] == 2
        assert section["resolved"] == 1
        assert section["open"] == 1
        assert section["dispositions"] == {"true_positive": 1}
        assert section["sla_met"] is True
        assert section["median_hours_to_disposition"] is not None
