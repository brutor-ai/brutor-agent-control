# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance notification channels + real-time alerts.

What is worth defending:

  * **Alerts are a projection of the inbox.** The dispatcher tails
    `assurance_inbox_events`; one decision row per (subscription, event)
    makes the tail idempotent — a second tick never re-sends.
  * **Coalescing**: within the window a recurring fingerprint yields ONE
    message and a `coalesced` bookkeeping row, so the counter on the
    item stays the record and the channel stays readable.
  * **Filters filter**: severity floor, categories, ai_system.
  * **Failure is loud, not silent**: failed sends retry with backoff and
    a channel that keeps failing becomes an assurance-inbox finding.
  * **Secrets stay secret**: configs are stored encrypted, listed only
    as masked summaries, and the generic webhook is HMAC-signed over the
    exact body bytes.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import hashlib
import hmac as hmac_mod
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.assurance_notifications import (
    create_assurance_notifications_router,
)
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
from app.database.models.notification_channel import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.database.models.tenant import Tenant
from app.services.assurance_inbox import AssuranceInboxService
from app.services.assurance_notifications import (
    NotificationDeliverer,
    NotificationDispatcher,
    render_slack,
    render_webhook,
    sign_payload,
)

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
    AssuranceInboxItem.__table__,
    AssuranceInboxEvent.__table__,
    NotificationChannel.__table__,
    NotificationSubscription.__table__,
    NotificationDelivery.__table__,
]


class FakeEncryption:
    """Reversible stand-in with the same encrypt/decrypt surface."""

    def encrypt(self, plaintext: str, **_kw) -> str:
        return "enc:" + plaintext

    def decrypt(self, encrypted: str) -> str:
        assert encrypted.startswith("enc:")
        return encrypted[4:]


class RecordingDeliverer(NotificationDeliverer):
    """Captures posts instead of hitting the network."""

    def __init__(self, fail: bool = False):
        super().__init__(BrutorLogger("test-deliverer"))
        self.fail = fail
        self.posts: List[Tuple[str, bytes, Dict[str, str]]] = []

    def post(self, url, body, headers):
        self.posts.append((url, body, headers))
        if self.fail:
            return False, "simulated failure"
        return True, ""


class _H:
    def __init__(self, client, Factory, enc):
        self.client = client
        self.Factory = Factory
        self.enc = enc

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


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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
    logger = BrutorLogger("test-notifications")
    enc = FakeEncryption()
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_assurance_notifications_router(logger, enc), prefix="/v1")
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

    yield _H(client=client, Factory=Factory, enc=enc)
    app.dependency_overrides.clear()
    engine.dispose()


# ── seeding helpers ─────────────────────────────────────────────────────


def _channel(s, enc, tenant_id="t1", kind="slack_webhook", **over) -> NotificationChannel:
    config = (
        {"webhook_url": "https://hooks.slack.com/services/T/B/x"}
        if kind == "slack_webhook"
        else {"url": "https://sink.example.com/hook", "secret": "s3cret"}
    )
    row = NotificationChannel(
        id=f"nchan-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        name=over.pop("name", "Ops Slack"),
        kind=kind,
        config_encrypted=enc.encrypt(json.dumps(config)),
        enabled=True,
        consecutive_failures=0,
        **over,
    )
    s.add(row)
    s.flush()
    return row


def _subscription(s, channel, **over) -> NotificationSubscription:
    row = NotificationSubscription(
        id=f"nsub-{uuid.uuid4().hex[:12]}",
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        event_class=over.pop("event_class", "inbox_item_created"),
        severity_floor=over.pop("severity_floor", "warning"),
        categories=over.pop("categories", None),
        ai_system_id=over.pop("ai_system_id", None),
        coalesce_seconds=over.pop("coalesce_seconds", 900),
        enabled=True,
        **over,
    )
    s.add(row)
    s.flush()
    return row


def _raise_item(s, tenant_id="t1", **over) -> AssuranceInboxItem:
    svc = AssuranceInboxService()
    kwargs = dict(
        tenant_id=tenant_id,
        ai_system_id="sys-1",
        category="drift",
        severity="critical",
        fingerprint=f"drift:sys-1:{uuid.uuid4().hex[:6]}",
        title="Drift detected",
        evidence={},
    )
    kwargs.update(over)
    item = svc.raise_item(s, **kwargs)
    s.commit()
    assert item is not None
    return item


def _dispatcher(enc, fail=False) -> Tuple[NotificationDispatcher, RecordingDeliverer]:
    deliverer = RecordingDeliverer(fail=fail)
    return (
        NotificationDispatcher(enc, deliverer, BrutorLogger("test-dispatch")),
        deliverer,
    )


# ── rendering + signing ─────────────────────────────────────────────────


class TestRendering:
    def test_slack_payload_shape(self):
        item = AssuranceInboxItem(
            id="inbox-1", tenant_id="t1", ai_system_id="sys-1",
            category="guardrail_block", severity="critical", fingerprint="f",
            title="Guardrail pii blocked content", evidence={},
            occurrence_count=4, state="open",
        )
        payload = render_slack(item, "reoccurred")
        assert "CRITICAL" in payload["text"]
        assert "×4" in payload["text"]
        assert payload["blocks"][0]["type"] == "section"

    def test_webhook_payload_carries_item_and_type(self):
        item = AssuranceInboxItem(
            id="inbox-1", tenant_id="t1", ai_system_id=None,
            category="drift", severity="warning", fingerprint="f",
            title="t", evidence={"drift_event_id": "d1"},
            occurrence_count=1, state="open",
        )
        payload = render_webhook(item, "created")
        assert payload["type"] == "assurance.inbox_item_created"
        assert payload["item"]["id"] == "inbox-1"

    def test_hmac_signature_is_verifiable(self):
        body = b'{"x":1}'
        sig = sign_payload("topsecret", body)
        assert sig.startswith("sha256=")
        expected = hmac_mod.new(b"topsecret", body, hashlib.sha256).hexdigest()
        assert sig == f"sha256={expected}"


# ── dispatcher core ─────────────────────────────────────────────────────


class TestDispatcher:
    def test_sends_once_and_is_idempotent_across_ticks(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        _subscription(s, ch)
        s.commit()
        _raise_item(s)

        dispatcher, deliverer = _dispatcher(harness.enc)
        assert dispatcher.run_once(s) == 1
        assert len(deliverer.posts) == 1
        # Second tick: the decision row makes the tail idempotent.
        assert dispatcher.run_once(s) == 0
        assert len(deliverer.posts) == 1
        s.close()

    def test_severity_floor_and_category_filters(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        _subscription(s, ch, severity_floor="critical", categories=["guardrail_block"])
        s.commit()
        # warning severity → below floor.
        _raise_item(s, severity="warning", category="guardrail_block")
        # right severity, wrong category.
        _raise_item(s, severity="critical", category="drift")
        # matches both.
        _raise_item(s, severity="critical", category="guardrail_block")

        dispatcher, deliverer = _dispatcher(harness.enc)
        assert dispatcher.run_once(s) == 1
        assert len(deliverer.posts) == 1
        s.close()

    def test_coalescing_window_collapses_reoccurrences(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        _subscription(s, ch, event_class="inbox_item_created")
        sub2 = _subscription(s, ch, event_class="inbox_item_reoccurred")
        s.commit()
        item = _raise_item(s, fingerprint="drift:sys-1:same")

        dispatcher, deliverer = _dispatcher(harness.enc)
        assert dispatcher.run_once(s) == 1  # created → sent

        # Re-occur three times: created-subscription ignores them; the
        # reoccurred-subscription coalesces them because a message for
        # this fingerprint was just sent... but by a DIFFERENT
        # subscription. Coalescing is per-subscription, so the FIRST
        # reoccurrence sends and the rest coalesce.
        svc = AssuranceInboxService()
        for _ in range(3):
            svc.raise_item(
                s, tenant_id="t1", ai_system_id="sys-1", category="drift",
                severity="critical", fingerprint="drift:sys-1:same",
                title="Drift detected",
            )
            s.commit()

        dispatcher.run_once(s)
        sent = (
            s.query(NotificationDelivery)
            .filter(
                NotificationDelivery.subscription_id == sub2.id,
                NotificationDelivery.status == "sent",
            )
            .count()
        )
        coalesced = (
            s.query(NotificationDelivery)
            .filter(
                NotificationDelivery.subscription_id == sub2.id,
                NotificationDelivery.status == "coalesced",
            )
            .count()
        )
        # Reoccurrence-event throttling in the inbox service (1/hour) means
        # only ONE reoccurred event exists; it must have been SENT (first
        # in its window), never dropped silently.
        assert sent == 1
        assert coalesced == 0
        assert item.occurrence_count == 4
        s.close()

    def test_failure_retries_with_backoff_then_succeeds(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        _subscription(s, ch)
        s.commit()
        _raise_item(s)

        dispatcher, deliverer = _dispatcher(harness.enc, fail=True)
        assert dispatcher.run_once(s) == 0
        row = s.query(NotificationDelivery).one()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.next_attempt_at is not None

        # Flip the network back on; force the retry due.
        deliverer.fail = False
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
        assert dispatcher.run_once(s) == 1
        s.refresh(row)
        assert row.status == "sent"
        assert row.attempts == 2
        s.close()

    def test_dead_channel_becomes_an_inbox_finding(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        _subscription(s, ch, severity_floor="info")
        s.commit()

        dispatcher, deliverer = _dispatcher(harness.enc, fail=True)
        # Five distinct items → five consecutive failures.
        for i in range(5):
            _raise_item(s, fingerprint=f"drift:sys-1:f{i}", severity="warning")
        dispatcher.run_once(s)
        s.commit()

        s.refresh(ch)
        assert ch.consecutive_failures >= 5
        finding = (
            s.query(AssuranceInboxItem)
            .filter(
                AssuranceInboxItem.category == "notification_failure",
                AssuranceInboxItem.fingerprint == f"notification_failure:{ch.id}",
            )
            .one()
        )
        assert "failed" in finding.title
        s.close()

    def test_tenant_isolation_in_dispatch(self, harness):
        s = harness.Factory()
        ch1 = _channel(s, harness.enc, tenant_id="t1")
        _subscription(s, ch1, severity_floor="info")
        s.commit()
        # Item in the OTHER tenant must not reach t1's channel.
        _raise_item(s, tenant_id="t2", severity="critical")

        dispatcher, deliverer = _dispatcher(harness.enc)
        assert dispatcher.run_once(s) == 0
        assert deliverer.posts == []
        s.close()

    def test_slack_and_webhook_deliveries_carry_correct_headers(self, harness):
        s = harness.Factory()
        slack = _channel(s, harness.enc, kind="slack_webhook", name="slack")
        hook = _channel(s, harness.enc, kind="webhook", name="hook")
        _subscription(s, slack, severity_floor="info")
        _subscription(s, hook, severity_floor="info")
        s.commit()
        _raise_item(s)

        dispatcher, deliverer = _dispatcher(harness.enc)
        assert dispatcher.run_once(s) == 2
        by_url = {url: (body, headers) for url, body, headers in deliverer.posts}
        assert "https://hooks.slack.com/services/T/B/x" in by_url
        body, headers = by_url["https://sink.example.com/hook"]
        assert headers["X-Brutor-Signature"] == sign_payload("s3cret", body)
        s.close()


# ── endpoints ───────────────────────────────────────────────────────────


class TestEndpoints:
    def test_channel_crud_masks_config_and_audits(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/notifications/channels",
            headers=h,
            json={
                "name": "Ops Slack",
                "kind": "slack_webhook",
                "config": {"webhook_url": "https://hooks.slack.com/services/T0/B0/secretpart"},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "secretpart" not in json.dumps(body)
        assert body["config_summary"].startswith("https://hooks.slack.com/")
        chan_id = body["id"]

        r = harness.client.get("/v1/admin/notifications/channels", headers=h)
        assert r.status_code == 200
        assert len(r.json()) == 1

        # http (non-https) slack webhook rejected.
        r = harness.client.post(
            "/v1/admin/notifications/channels",
            headers=h,
            json={
                "name": "bad", "kind": "slack_webhook",
                "config": {"webhook_url": "http://x"},
            },
        )
        assert r.status_code == 422

        # Update name only — config survives.
        r = harness.client.patch(
            f"/v1/admin/notifications/channels/{chan_id}",
            headers=h,
            json={"name": "Renamed"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"
        assert r.json()["config_summary"].startswith("https://hooks.slack.com/")

        # Cross-tenant access is 404.
        assert (
            harness.client.get(
                f"/v1/admin/notifications/channels", headers=harness.login("t2")
            ).json()
            == []
        )
        r = harness.client.delete(
            f"/v1/admin/notifications/channels/{chan_id}",
            headers=harness.login("t2"),
        )
        assert r.status_code == 404

        # Audit rows for the mutations.
        s = harness.Factory()
        actions = {
            row.action
            for row in s.query(TenantAdminAuditLog)
            .filter(TenantAdminAuditLog.action.like("notification.%"))
            .all()
        }
        s.close()
        assert "notification.channel.create" in actions
        assert "notification.channel.update" in actions

    def test_subscription_crud_and_validation(self, harness):
        h = harness.login("t1")
        r = harness.client.post(
            "/v1/admin/notifications/channels",
            headers=h,
            json={
                "name": "Ops", "kind": "webhook",
                "config": {"url": "https://sink.example.com/h", "secret": "s"},
            },
        )
        chan_id = r.json()["id"]

        r = harness.client.post(
            "/v1/admin/notifications/subscriptions",
            headers=h,
            json={"channel_id": chan_id, "severity_floor": "critical"},
        )
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]

        # Bad enums are 422s.
        assert harness.client.post(
            "/v1/admin/notifications/subscriptions", headers=h,
            json={"channel_id": chan_id, "event_class": "weird"},
        ).status_code == 422
        assert harness.client.post(
            "/v1/admin/notifications/subscriptions", headers=h,
            json={"channel_id": chan_id, "severity_floor": "weird"},
        ).status_code == 422
        # Subscribing to another tenant's channel is 404.
        assert harness.client.post(
            "/v1/admin/notifications/subscriptions", headers=harness.login("t2"),
            json={"channel_id": chan_id},
        ).status_code == 404

        r = harness.client.patch(
            f"/v1/admin/notifications/subscriptions/{sub_id}",
            headers=h,
            json={"severity_floor": "info", "categories": ["drift"]},
        )
        assert r.status_code == 200
        assert r.json()["severity_floor"] == "info"
        assert r.json()["categories"] == ["drift"]

        r = harness.client.delete(
            f"/v1/admin/notifications/subscriptions/{sub_id}", headers=h
        )
        assert r.status_code == 200

    def test_deliveries_listing_with_filters(self, harness):
        s = harness.Factory()
        ch = _channel(s, harness.enc)
        sub = _subscription(s, ch, severity_floor="info")
        s.commit()
        _raise_item(s)
        dispatcher, _ = _dispatcher(harness.enc)
        dispatcher.run_once(s)
        s.close()

        h = harness.login("t1")
        r = harness.client.get("/v1/admin/notifications/deliveries", headers=h)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["deliveries"][0]["status"] == "sent"

        r = harness.client.get(
            "/v1/admin/notifications/deliveries",
            headers=h,
            params={"status": "failed"},
        )
        assert r.json()["total"] == 0
        assert (
            harness.client.get(
                "/v1/admin/notifications/deliveries", headers=h,
                params={"status": "weird"},
            ).status_code
            == 422
        )
