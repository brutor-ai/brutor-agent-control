# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Recording that an admin session is still in use, without holding a lock.

`verify_token` runs at the start of every admin request. It used to set
`last_activity` on the session row inside the request's own transaction, so
the row stayed locked until that request finished. The console sends requests
in bursts; once the 60-second throttle expired, every request of the burst
queued behind the slowest one for the same row. A queued request that hit the
statement timeout failed inside `verify_token`, which answers 401, and the
console signed the admin out (a long report query can hold the row for
seconds while other requests of the same session wait on it).

Rules this module holds:

* The touch is one conditional UPDATE in its **own** transaction on its own
  connection, committed at once. The request's transaction never writes the
  session row.
* The UPDATE is conditional on the row still being stale, so of N concurrent
  touches one writes and the rest change nothing.
* On Postgres it runs under a short `lock_timeout`: if something does hold
  the row, the touch gives up rather than wait.
* It never fails the request. `last_activity` is bookkeeping; whether the
  session is valid was decided before the touch.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_, text, update
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger

#: A session's `last_activity` is written at most this often.
ACTIVITY_TOUCH_INTERVAL_SECONDS = 60

#: How long the touch may wait for the row before giving up (Postgres).
TOUCH_LOCK_TIMEOUT_MS = 200

_logger = BrutorLogger("session-activity")


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    # Postgres returns aware datetimes; imported rows and SQLite can be naive.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_stale(last_activity: Optional[datetime], now: datetime) -> bool:
    last = _aware(last_activity)
    return last is None or (now - last).total_seconds() >= ACTIVITY_TOUCH_INTERVAL_SECONDS


def touch_session_activity(db: Session, session_row: Any, *, now: Optional[datetime] = None) -> bool:
    """Record activity on an admin session row (system or tenant).

    Returns True when this call wrote the timestamp. Never raises and never
    writes through `db`: the request's transaction stays free of the row.
    """
    now = now or datetime.now(timezone.utc)
    if not is_stale(session_row.last_activity, now):
        return False
    table = type(session_row).__table__
    cutoff = now - timedelta(seconds=ACTIVITY_TOUCH_INTERVAL_SECONDS)
    try:
        engine = db.get_bind().engine
        with engine.connect() as conn:
            with conn.begin():
                if engine.dialect.name == "postgresql":
                    conn.execute(text(f"SET LOCAL lock_timeout = '{TOUCH_LOCK_TIMEOUT_MS}ms'"))
                result = conn.execute(
                    update(table)
                    .where(
                        table.c.id == session_row.id,
                        or_(table.c.last_activity.is_(None), table.c.last_activity < cutoff),
                    )
                    .values(last_activity=now)
                )
                return (result.rowcount or 0) > 0
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail authentication
        _logger.debug(
            "Session activity not recorded",
            context={"session_id": session_row.id, "error": str(exc).split("\n")[0][:200]},
        )
        return False


__all__ = [
    "ACTIVITY_TOUCH_INTERVAL_SECONDS",
    "TOUCH_LOCK_TIMEOUT_MS",
    "is_stale",
    "touch_session_activity",
]
