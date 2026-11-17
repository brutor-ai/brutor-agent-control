# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Weekly digest scheduler.

Every 5 minutes: for each tenant with an enabled ``digest_weekly``
subscription, check whether this week's send point (default Monday 08:00
tenant-local, from ``Tenant.settings["assurance_digest"]``) has passed.
If so, generate-once (unique ``assurance_digests`` row per tenant+week)
and deliver to every digest subscription that has not yet received it —
per-subscription idempotency rides the same
``(subscription_id, inbox_event_id)`` unique key the alert dispatcher
uses, with the digest row's id standing in for the event id — claimed
by an ``ON CONFLICT DO NOTHING`` insert *before* the send, so the
unique index decides who delivers rather than merely recording who did.

Digest sends do NOT retry within the week (the next digest is the
catch-up; a failing channel still accrues its failure streak and
surfaces as a ``notification_failure`` finding).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.database.models.assurance_check import AssuranceDigest
from app.database.models.notification_channel import (
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    EVENT_CLASS_DIGEST,
    MAX_DELIVERY_ATTEMPTS,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.database.models.tenant import Tenant
from app.services.assurance_digest import (
    build_digest,
    digest_due,
    digest_schedule,
    store_digest,
)
from app.services.assurance_notifications import NotificationDispatcher
from app.services.scheduler_leader import run_as_leader
from ulid import ULID

log = logging.getLogger("brutor.assurance-digest")

TICK_INTERVAL_SECONDS = 300

_dispatcher: Optional[NotificationDispatcher] = None


def run_once(*, now: Optional[datetime] = None) -> int:
    """One tick; returns the number of digest deliveries attempted."""
    if _dispatcher is None:
        return 0
    now = now or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        return _run(db, now)
    finally:
        db.close()


def _run(db: Session, now: datetime) -> int:
    sent = 0
    subscription_rows = (
        db.query(NotificationSubscription, NotificationChannel)
        .join(
            NotificationChannel,
            NotificationSubscription.channel_id == NotificationChannel.id,
        )
        .filter(
            NotificationSubscription.event_class == EVENT_CLASS_DIGEST,
            NotificationSubscription.enabled.is_(True),
            NotificationChannel.enabled.is_(True),
        )
        .all()
    )
    if not subscription_rows:
        return 0

    by_tenant: dict[str, list] = {}
    for sub, channel in subscription_rows:
        by_tenant.setdefault(sub.tenant_id, []).append((sub, channel))

    for tenant_id, pairs in by_tenant.items():
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        schedule = digest_schedule(tenant)
        week_start = digest_due(now, schedule)
        if week_start is None:
            continue

        digest_row = (
            db.query(AssuranceDigest)
            .filter(
                AssuranceDigest.tenant_id == tenant_id,
                AssuranceDigest.week_start == week_start,
            )
            .first()
        )
        if digest_row is None:
            try:
                content = build_digest(db, tenant_id, week_start)
                digest_row = store_digest(db, tenant_id, week_start, content)
                db.commit()
            except Exception:  # noqa: BLE001 — one tenant must not stop the rest
                db.rollback()
                log.exception("digest build failed for tenant %s", tenant_id)
                continue
            if digest_row is None:
                # Concurrent instance won the insert — reload.
                digest_row = (
                    db.query(AssuranceDigest)
                    .filter(
                        AssuranceDigest.tenant_id == tenant_id,
                        AssuranceDigest.week_start == week_start,
                    )
                    .first()
                )
                if digest_row is None:
                    continue

        for sub, channel in pairs:
            # Claim the (subscription, digest) slot BEFORE delivering.
            #
            # The old shape was SELECT-then-send-then-INSERT. The unique
            # index was already there, so the loser's INSERT did fail —
            # but only after it had emailed the subscriber. Losing the
            # race at the insert is no use when the side effect happens
            # first; the insert has to be what decides who sends.
            if not _claim_delivery(db, tenant_id, sub, channel, digest_row):
                continue
            try:
                config = _dispatcher.decrypt_config(channel)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "digest: channel %s config unreadable: %s", channel.id, exc
                )
                _finish_delivery(
                    db, sub.id, digest_row.id, now,
                    ok=False, error=f"channel config unreadable: {exc}",
                )
                continue
            ok, error = _dispatcher.deliverer.deliver_digest(
                channel.kind, config, digest_row.content
            )
            sent += 1
            _finish_delivery(db, sub.id, digest_row.id, now, ok=ok, error=error)
            if ok:
                _dispatcher._channel_ok(channel, now)
            else:
                _dispatcher._channel_failed(db, channel, now, error)
            db.commit()
    return sent


def _claim_delivery(db: Session, tenant_id: str, sub, channel, digest_row) -> bool:
    """Insert the delivery row first; True means this worker owns the send.

    `ON CONFLICT DO NOTHING` against `uq_ndel_subscription_event` turns the
    unique index into the arbiter. A concurrent worker — or a re-entered
    tick after a crash — gets False and sends nothing.
    """
    stmt = (
        pg_insert(NotificationDelivery.__table__)
        .values(
            id=f"ndel-{ULID()}",
            tenant_id=tenant_id,
            subscription_id=sub.id,
            channel_id=channel.id,
            inbox_event_id=digest_row.id,
            inbox_item_id=digest_row.id,
            fingerprint=f"digest_weekly:{digest_row.week_start.isoformat()}",
            status=DELIVERY_PENDING,
            # No within-week retry: MAX attempts keeps the alert
            # dispatcher's retry loop off digest rows.
            attempts=MAX_DELIVERY_ATTEMPTS,
        )
        .on_conflict_do_nothing(
            index_elements=["subscription_id", "inbox_event_id"]
        )
    )
    claimed = db.execute(stmt).rowcount > 0
    # Commit the claim on its own so the other worker sees it before we
    # start the (slow, network-bound) delivery.
    db.commit()
    return claimed


def _finish_delivery(
    db: Session,
    subscription_id: str,
    digest_id: str,
    now: datetime,
    *,
    ok: bool,
    error: Optional[str],
) -> None:
    """Record the outcome on the row we claimed."""
    (
        db.query(NotificationDelivery)
        .filter(
            NotificationDelivery.subscription_id == subscription_id,
            NotificationDelivery.inbox_event_id == digest_id,
        )
        .update(
            {
                "status": DELIVERY_SENT if ok else DELIVERY_FAILED,
                "last_error": None if ok else error,
                "sent_at": now if ok else None,
            },
            synchronize_session=False,
        )
    )
    db.commit()


async def assurance_digest_loop() -> None:
    while True:
        try:
            # Leader election keeps two in-phase workers (forked at the
            # same instant, sleeping the same interval) from both walking
            # the tenant list. The per-subscriber claim below is the
            # backstop for when the lease itself changes hands mid-tick:
            # it is the unique index, not a preceding SELECT, that decides
            # who gets to send.
            sent = await asyncio.to_thread(
                run_as_leader, "assurance_digest", run_once
            )
            if sent:
                log.info("assurance digest tick: %d deliveries", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("assurance digest: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_assurance_digest_scheduler(encryption_service) -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup, on the lead instance."""
    global _dispatcher
    _dispatcher = NotificationDispatcher(encryption_service)
    return asyncio.create_task(
        assurance_digest_loop(), name="assurance-digest-scheduler"
    )
