# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance notification dispatcher.

Design invariants:

* **Alerts are a projection of the inbox.** The dispatcher tails
  ``assurance_inbox_events`` — the one table BOTH the control plane and
  the Rust core write — so an alert can never disagree with what the
  inbox shows. No second event pipeline, no core→CP plumbing.
* **One decision row per (subscription, event).** ``sent`` / ``failed`` /
  ``coalesced`` / ``skipped`` — the unique index makes the tail
  idempotent, and the rows double as the delivery audit trail.
* **Coalescing per fingerprint**: within a subscription's window, a
  recurring finding produces ONE message; the occurrence counter on the
  inbox item stays the precise record.
* **Fail-open, loudly**: delivery failures never block anything; they
  retry with exponential backoff, and a channel that fails
  ``DEAD_CHANNEL_THRESHOLD`` times in a row becomes an assurance-inbox
  finding itself — a dead alert channel is a silent-failure mode.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.outbound_http import outbound_client
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.assurance_inbox import (
    AssuranceInboxEvent,
    AssuranceInboxItem,
)
from app.database.models.notification_channel import (
    CHANNEL_KIND_SLACK,
    CHANNEL_KIND_WEBHOOK,
    DEAD_CHANNEL_THRESHOLD,
    DELIVERY_COALESCED,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    EVENT_CLASS_CREATED,
    EVENT_CLASS_REOCCURRED,
    MAX_DELIVERY_ATTEMPTS,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.services.assurance_inbox import AssuranceInboxService

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# Only events younger than this are considered — a dispatcher that was down
# for a day must not flood channels with stale findings on restart; the
# weekly digest is the catch-up surface.
LOOKBACK_MINUTES = 30

HTTP_TIMEOUT_SECONDS = 10.0

_EVENT_TO_CLASS = {
    "created": EVENT_CLASS_CREATED,
    "reoccurred": EVENT_CLASS_REOCCURRED,
}

SEVERITY_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{ULID()}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── Rendering ───────────────────────────────────────────────────────────


def render_slack(item: AssuranceInboxItem, event_kind: str) -> Dict[str, Any]:
    """Slack incoming-webhook payload. Kept to `text` + simple blocks so it
    renders identically in Slack-compatible sinks (Mattermost, etc.)."""
    emoji = SEVERITY_EMOJI.get(item.severity, "")
    reoccur = (
        f" (×{item.occurrence_count} since first seen)"
        if event_kind == "reoccurred"
        else ""
    )
    text = f"{emoji} [{item.severity.upper()}] {item.title}{reoccur}"
    fields = [
        {"type": "mrkdwn", "text": f"*Category:*\n{item.category}"},
        {"type": "mrkdwn", "text": f"*State:*\n{item.state}"},
    ]
    if item.ai_system_id:
        fields.append(
            {"type": "mrkdwn", "text": f"*AI System:*\n{item.ai_system_id}"}
        )
    fields.append(
        {"type": "mrkdwn", "text": f"*Occurrences:*\n{item.occurrence_count}"}
    )
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
            {"type": "section", "fields": fields},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"Assurance Inbox item `{item.id}` — review and "
                            "record a disposition in Mission Control."
                        ),
                    }
                ],
            },
        ],
    }


def render_webhook(item: AssuranceInboxItem, event_kind: str) -> Dict[str, Any]:
    """Generic webhook payload: the inbox item verbatim + event metadata.
    Consumers (n8n / Zapier / SIEM) get pointers, never copied content."""
    return {
        "type": f"assurance.inbox_item_{event_kind}",
        "occurred_at": _now().isoformat(),
        "item": item.to_dict(),
    }


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 signature for generic webhooks:
    `X-Brutor-Signature: sha256=<hex>` over the exact request body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def render_digest_slack(content: Dict[str, Any]) -> Dict[str, Any]:
    """Slack payload for the weekly digest. The numbers
    are the stored projection — never recomputed at render time."""
    totals = content.get("totals", {})
    week = content.get("week_start", "")
    text = (
        f"📋 Weekly assurance digest — week of {week}: "
        f"{totals.get('items_raised', 0)} raised, "
        f"{totals.get('items_resolved', 0)} resolved, "
        f"{totals.get('open_critical', 0)} critical open"
    )
    system_lines = []
    for system in (content.get("systems") or [])[:8]:
        state = system.get("health_state", "unknown")
        emoji = {"healthy": "✅"}.get(state, "⚠️")
        system_lines.append(
            f"{emoji} *{system.get('name')}* — {state}; "
            f"{system.get('runs', 0)} runs, "
            f"${system.get('cost_usd', 0)}, "
            f"{system.get('open_critical', 0)} critical open"
        )
    blocks: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
    ]
    if system_lines:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(system_lines)},
            }
        )
    breaches = content.get("top_check_breaches") or []
    if breaches:
        breach_lines = [
            f"• [{b.get('severity', '?').upper()}] {b.get('title')} "
            f"(×{b.get('occurrences', 1)})"
            for b in breaches[:5]
        ]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Top check breaches:*\n" + "\n".join(breach_lines),
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Full detail: Mission Control → Weekly summary.",
                }
            ],
        }
    )
    return {"text": text, "blocks": blocks}


def render_digest_webhook(content: Dict[str, Any]) -> Dict[str, Any]:
    """Generic webhook payload for the weekly digest."""
    return {
        "type": "assurance.digest_weekly",
        "occurred_at": _now().isoformat(),
        "digest": content,
    }


# ── Delivery ────────────────────────────────────────────────────────────


class NotificationDeliverer:
    """The HTTP edge, isolated so tests monkeypatch `post`."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("assurance-notify")

    def post(
        self, url: str, body: bytes, headers: Dict[str, str]
    ) -> Tuple[bool, str]:
        try:
            # A channel's URL is an administrator's destination.
            with outbound_client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                resp = client.post(url, content=body, headers=headers)
            if 200 <= resp.status_code < 300:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:  # noqa: BLE001 — network edge, fail-open
            return False, str(e)[:500]

    def deliver(
        self,
        channel_kind: str,
        config: Dict[str, Any],
        item: AssuranceInboxItem,
        event_kind: str,
    ) -> Tuple[bool, str]:
        if channel_kind == CHANNEL_KIND_SLACK:
            url = config.get("webhook_url", "")
            if not url:
                return False, "channel config has no webhook_url"
            body = json.dumps(render_slack(item, event_kind)).encode("utf-8")
            return self.post(url, body, {"Content-Type": "application/json"})
        if channel_kind == CHANNEL_KIND_WEBHOOK:
            url = config.get("url", "")
            if not url:
                return False, "channel config has no url"
            body = json.dumps(render_webhook(item, event_kind)).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            secret = config.get("secret")
            if secret:
                headers["X-Brutor-Signature"] = sign_payload(secret, body)
            return self.post(url, body, headers)
        return False, f"unknown channel kind '{channel_kind}'"

    def deliver_digest(
        self,
        channel_kind: str,
        config: Dict[str, Any],
        content: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Send a weekly digest over a channel."""
        if channel_kind == CHANNEL_KIND_SLACK:
            url = config.get("webhook_url", "")
            if not url:
                return False, "channel config has no webhook_url"
            body = json.dumps(render_digest_slack(content)).encode("utf-8")
            return self.post(url, body, {"Content-Type": "application/json"})
        if channel_kind == CHANNEL_KIND_WEBHOOK:
            url = config.get("url", "")
            if not url:
                return False, "channel config has no url"
            body = json.dumps(render_digest_webhook(content)).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            secret = config.get("secret")
            if secret:
                headers["X-Brutor-Signature"] = sign_payload(secret, body)
            return self.post(url, body, headers)
        return False, f"unknown channel kind '{channel_kind}'"


# ── Dispatcher ──────────────────────────────────────────────────────────


class NotificationDispatcher:
    def __init__(
        self,
        encryption_service,
        deliverer: Optional[NotificationDeliverer] = None,
        logger: Optional[BrutorLogger] = None,
    ):
        self.encryption = encryption_service
        self.deliverer = deliverer or NotificationDeliverer(logger)
        self.logger = logger or BrutorLogger("assurance-notify")
        self.inbox = AssuranceInboxService(self.logger)

    # -- channel config ------------------------------------------------

    def decrypt_config(self, channel: NotificationChannel) -> Dict[str, Any]:
        return json.loads(self.encryption.decrypt(channel.config_encrypted))

    # -- the tick ------------------------------------------------------

    def run_once(self, db: Session, *, now: Optional[datetime] = None) -> int:
        """One dispatch tick: tail fresh inbox events, decide per
        subscription, send, then retry previously failed deliveries.
        Returns the number of messages actually sent."""
        now = now or _now()
        sent = self._dispatch_new_events(db, now)
        sent += self._retry_failed(db, now)
        return sent

    def _dispatch_new_events(self, db: Session, now: datetime) -> int:
        since = now - timedelta(minutes=LOOKBACK_MINUTES)
        rows: List[Tuple[AssuranceInboxEvent, AssuranceInboxItem]] = (
            db.query(AssuranceInboxEvent, AssuranceInboxItem)
            .join(
                AssuranceInboxItem,
                AssuranceInboxItem.id == AssuranceInboxEvent.item_id,
            )
            .filter(
                AssuranceInboxEvent.at >= since,
                AssuranceInboxEvent.event.in_(("created", "reoccurred")),
            )
            .order_by(AssuranceInboxEvent.at.asc())
            .all()
        )
        if not rows:
            return 0

        tenants = {ev.tenant_id for ev, _ in rows}
        subs_by_tenant: Dict[str, List[Tuple[NotificationSubscription, NotificationChannel]]] = {}
        for tenant_id in tenants:
            subs_by_tenant[tenant_id] = (
                db.query(NotificationSubscription, NotificationChannel)
                .join(
                    NotificationChannel,
                    NotificationChannel.id == NotificationSubscription.channel_id,
                )
                .filter(
                    NotificationSubscription.tenant_id == tenant_id,
                    NotificationSubscription.enabled.is_(True),
                    NotificationChannel.enabled.is_(True),
                )
                .all()
            )

        sent = 0
        for event, item in rows:
            for sub, channel in subs_by_tenant.get(event.tenant_id, []):
                if not self._matches(sub, event, item):
                    continue
                if self._already_decided(db, sub.id, event.id):
                    continue
                if self._coalesced(db, sub, item, now):
                    self._record(
                        db, sub, channel, event, item,
                        status=DELIVERY_COALESCED, now=now,
                    )
                    continue
                ok, error = self._send(channel, item, event.event)
                if ok:
                    self._record(
                        db, sub, channel, event, item,
                        status=DELIVERY_SENT, now=now,
                    )
                    self._channel_ok(channel, now)
                    sent += 1
                else:
                    self._record(
                        db, sub, channel, event, item,
                        status=DELIVERY_FAILED, now=now, error=error, attempts=1,
                    )
                    self._channel_failed(db, channel, now, error)
            db.commit()
        return sent

    def _retry_failed(self, db: Session, now: datetime) -> int:
        rows = (
            db.query(NotificationDelivery)
            .filter(
                NotificationDelivery.status == DELIVERY_FAILED,
                NotificationDelivery.attempts < MAX_DELIVERY_ATTEMPTS,
                NotificationDelivery.next_attempt_at <= now,
            )
            .order_by(NotificationDelivery.next_attempt_at.asc())
            .limit(50)
            .all()
        )
        sent = 0
        for delivery in rows:
            channel = db.get(NotificationChannel, delivery.channel_id)
            item = db.get(AssuranceInboxItem, delivery.inbox_item_id)
            if channel is None or item is None or not channel.enabled:
                delivery.attempts = MAX_DELIVERY_ATTEMPTS  # give up quietly
                continue
            ok, error = self._send(channel, item, "created")
            delivery.attempts += 1
            if ok:
                delivery.status = DELIVERY_SENT
                delivery.sent_at = now
                delivery.last_error = None
                delivery.next_attempt_at = None
                self._channel_ok(channel, now)
                sent += 1
            else:
                delivery.last_error = error
                delivery.next_attempt_at = now + timedelta(
                    minutes=2 ** delivery.attempts
                )
                self._channel_failed(db, channel, now, error)
            db.commit()
        return sent

    # -- decision helpers ----------------------------------------------

    def _matches(
        self,
        sub: NotificationSubscription,
        event: AssuranceInboxEvent,
        item: AssuranceInboxItem,
    ) -> bool:
        if _EVENT_TO_CLASS.get(event.event) != sub.event_class:
            return False
        if _SEVERITY_RANK.get(item.severity, 0) < _SEVERITY_RANK.get(
            sub.severity_floor, 1
        ):
            return False
        if sub.categories and item.category not in sub.categories:
            return False
        if sub.ai_system_id and item.ai_system_id != sub.ai_system_id:
            return False
        return True

    def _already_decided(self, db: Session, sub_id: str, event_id: str) -> bool:
        return (
            db.query(NotificationDelivery.id)
            .filter(
                NotificationDelivery.subscription_id == sub_id,
                NotificationDelivery.inbox_event_id == event_id,
            )
            .first()
            is not None
        )

    def _coalesced(
        self,
        db: Session,
        sub: NotificationSubscription,
        item: AssuranceInboxItem,
        now: datetime,
    ) -> bool:
        window_start = now - timedelta(seconds=sub.coalesce_seconds or 0)
        last_sent = (
            db.query(NotificationDelivery)
            .filter(
                NotificationDelivery.subscription_id == sub.id,
                NotificationDelivery.fingerprint == item.fingerprint,
                NotificationDelivery.status == DELIVERY_SENT,
                NotificationDelivery.sent_at >= window_start,
            )
            .first()
        )
        return last_sent is not None

    def _send(
        self, channel: NotificationChannel, item: AssuranceInboxItem, event_kind: str
    ) -> Tuple[bool, str]:
        try:
            config = self.decrypt_config(channel)
        except Exception as e:  # noqa: BLE001
            return False, f"config decrypt failed: {e}"
        return self.deliverer.deliver(channel.kind, config, item, event_kind)

    def _record(
        self,
        db: Session,
        sub: NotificationSubscription,
        channel: NotificationChannel,
        event: AssuranceInboxEvent,
        item: AssuranceInboxItem,
        *,
        status: str,
        now: datetime,
        error: Optional[str] = None,
        attempts: int = 0,
    ) -> None:
        db.add(
            NotificationDelivery(
                id=_new_id("ndel"),
                tenant_id=sub.tenant_id,
                subscription_id=sub.id,
                channel_id=channel.id,
                inbox_event_id=event.id,
                inbox_item_id=item.id,
                fingerprint=item.fingerprint,
                status=status,
                attempts=attempts,
                next_attempt_at=(
                    now + timedelta(minutes=2) if status == DELIVERY_FAILED else None
                ),
                last_error=error,
                sent_at=now if status == DELIVERY_SENT else None,
                created_at=now,
            )
        )

    # -- channel health ------------------------------------------------

    def _channel_ok(self, channel: NotificationChannel, now: datetime) -> None:
        channel.consecutive_failures = 0
        channel.last_success_at = now
        channel.last_error = None

    def _channel_failed(
        self, db: Session, channel: NotificationChannel, now: datetime, error: str
    ) -> None:
        channel.consecutive_failures = (channel.consecutive_failures or 0) + 1
        channel.last_failure_at = now
        channel.last_error = error
        if channel.consecutive_failures == DEAD_CHANNEL_THRESHOLD:
            # A dead alert channel is a silent-failure mode: the operator
            # believes they are covered while nothing is being delivered.
            self.inbox.raise_item(
                db,
                tenant_id=channel.tenant_id,
                category="notification_failure",
                severity="warning",
                fingerprint=f"notification_failure:{channel.id}",
                title=(
                    f"Notification channel '{channel.name}' has failed "
                    f"{channel.consecutive_failures} deliveries in a row"
                ),
                evidence={"channel_id": channel.id, "last_error": error[:500]},
            )
