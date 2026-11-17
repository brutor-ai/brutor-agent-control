# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Background loop for the assurance notification dispatcher. Same shape as the closed-loop / assurance schedulers: a
15-second tick, each tick in a worker thread with its own session, and a
tick that crashes never kills the loop — a notifier that silently stopped
notifying is the exact failure mode the dead-channel finding exists for,
and it would be embarrassing to ship it in the notifier itself.
"""

import asyncio
import logging

from app.database import SessionLocal
from app.services.assurance_notifications import NotificationDispatcher
from app.services.scheduler_leader import run_as_leader

log = logging.getLogger("brutor.notification-dispatcher")

TICK_INTERVAL_SECONDS = 15

_dispatcher: NotificationDispatcher | None = None


def run_once() -> int:
    if _dispatcher is None:
        return 0
    db = SessionLocal()
    try:
        return _dispatcher.run_once(db)
    finally:
        db.close()


async def notification_dispatcher_loop() -> None:
    while True:
        try:
            # Leader election — delivery is an external side effect
            # (Slack / webhook). Two workers tailing the same inbox
            # cursor deliver every alert twice.
            await asyncio.to_thread(
                run_as_leader, "assurance_notifications", run_once
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("notification dispatcher: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_notification_dispatcher(encryption_service) -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup, on the lead instance."""
    global _dispatcher
    _dispatcher = NotificationDispatcher(encryption_service)
    return asyncio.create_task(
        notification_dispatcher_loop(), name="assurance-notification-dispatcher"
    )
