# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Account lockout for administrators: one counter, one rule, one place.

Every step of a sign-in that checks something the caller must know — the
password (`UnifiedAuthService.login_tenant_admin`, the system administrator's
`login`), the second-step code (`app/api/v1/endpoints/admin/mfa.py`) and the
current password on `change-password` — counts a wrong answer on the
administrator's `failed_login_attempts` and locks the account at
`MAX_FAILED_ATTEMPTS`.

A count made with `+= 1` and a flush never reaches the database: the route
then refuses by raising, which rolls the request's session back, and the lock
would never engage. `count_failure` COMMITS:
a refusal is exactly the case in which the caller's transaction is thrown away.

The lock is timed. `is_locked` with a `locked_until` ends by itself when that
moment has passed: the next look at the account (`release_if_expired`)
clears it and the count. Without that, five wrong passwords from anyone who
can reach the sign-in form would shut the administrator out for good — a
Community installation has one, and no route unlocks an account. `is_locked`
with no `locked_until` is a lock an administrator set; it does not expire.

The count is incremented in the database (`failed_login_attempts =
failed_login_attempts + 1`), not from the value this session read, so guesses
made in parallel all count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import bcrypt
from sqlalchemy.orm import Session

#: Consecutive wrong answers (password, second-step code, current password)
#: after which the account locks.
MAX_FAILED_ATTEMPTS = 5
#: How long that lock lasts.
LOCKOUT_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    # SQLite hands a timezone-aware column back naive; the value is UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def currently_locked(row: Any, now: Optional[datetime] = None) -> bool:
    """Whether the account is locked at this moment. Reads only."""
    if not row.is_locked:
        return False
    until = getattr(row, "locked_until", None)
    return until is None or _aware(until) > (now or _now())


def release_if_expired(row: Any, now: Optional[datetime] = None) -> bool:
    """Clear a timed lock whose end has passed (the count starts again).
    Returns whether the account is locked now. The caller's transaction
    carries the change."""
    if not row.is_locked:
        return False
    if currently_locked(row, now):
        return True
    clear(row)
    return False


def clear(row: Any) -> None:
    """A clean slate: after a completed sign-in, an expired lock, an unlock."""
    row.failed_login_attempts = 0
    row.is_locked = False
    row.locked_until = None


def _lock(row: Any) -> None:
    row.is_locked = True
    row.locked_until = _now() + timedelta(minutes=LOCKOUT_MINUTES)


def lock_at_threshold(row: Any, db: Session, *, max_attempts: int = MAX_FAILED_ATTEMPTS) -> bool:
    """Lock (and COMMIT) an account whose count is at the threshold and which
    is not marked locked. Returns whether it locked the account."""
    if (row.failed_login_attempts or 0) < max_attempts:
        return False
    _lock(row)
    db.commit()
    return True


def count_failure(row: Any, db: Session, *, max_attempts: int = MAX_FAILED_ATTEMPTS) -> bool:
    """Count one wrong answer against `row`, lock the account at the
    threshold, and COMMIT. Returns whether the account is locked now."""
    model = type(row)
    # What this session already changed on the row (an expired lock just
    # released sets the count to 0) goes first: the increment below is
    # computed by the database from the stored value.
    db.flush()
    row.failed_login_attempts = model.failed_login_attempts + 1
    db.flush()
    db.refresh(row, ["failed_login_attempts"])
    locked = row.failed_login_attempts >= max_attempts
    if locked:
        _lock(row)
    db.commit()
    return locked


@lru_cache(maxsize=1)
def _placeholder_hash() -> bytes:
    return bcrypt.hashpw(b"no account has this password", bcrypt.gensalt())


def spend_one_verification(password: str) -> None:
    """What a sign-in that will be refused without looking at a stored hash
    (unknown user, locked or deactivated account) costs instead: one bcrypt
    verification of the same work factor, so the refusal takes as long as a
    wrong password does and the time says nothing about the account."""
    try:
        bcrypt.checkpw(password.encode("utf-8"), _placeholder_hash())
    except Exception:  # noqa: BLE001 — the result is never used
        pass
