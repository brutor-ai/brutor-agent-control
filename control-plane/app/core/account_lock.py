# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The answer to a caller whose account cannot be used: 423, saying which case
it is.

    {"detail": "<text>", "reason": "<reason>", "locked_until": "<ISO 8601>" | null,
     "lockout_minutes": 15}

`reason` is one of `REASONS`:

    timed_lock         locked after too many wrong answers; ends by itself at
                       `locked_until`
    lock_without_end   locked with no end; an administrator must unlock it
    deactivated        the account is deactivated

A route refuses with `raise refusal(row)` (or `refuse_if_unusable(row)`), and
declares the answer with `responses=RESPONSES`. `install(app)` makes the
application send the body above; the application factory calls it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.admin_user import lockout

TIMED_LOCK = "timed_lock"
LOCK_WITHOUT_END = "lock_without_end"
DEACTIVATED = "deactivated"
REASONS = (TIMED_LOCK, LOCK_WITHOUT_END, DEACTIVATED)


class AccountLockedResponse(BaseModel):
    """The body of a 423."""

    detail: str = Field(..., description="What happened and what ends it, in words")
    reason: Literal["timed_lock", "lock_without_end", "deactivated"] = Field(
        ...,
        description=(
            "`timed_lock`: locked after too many wrong answers, ends by itself at `locked_until`; "
            "`lock_without_end`: locked until an administrator unlocks the account; "
            "`deactivated`: the account is deactivated"
        ),
    )
    locked_until: Optional[str] = Field(
        None, description="When a `timed_lock` ends (ISO 8601, UTC); null for the other reasons"
    )
    lockout_minutes: int = Field(
        lockout.LOCKOUT_MINUTES, description="How long a lock after too many wrong answers lasts"
    )


#: For `responses=` on a route that can answer 423.
RESPONSES: Dict[int | str, Dict[str, Any]] = {
    423: {
        "model": AccountLockedResponse,
        "description": "The account is locked or deactivated; `reason` says which",
    }
}


class AccountLocked(HTTPException):
    """423 with a reason. Without the handler it is still a 423 with the text."""

    def __init__(self, reason: str, locked_until: Optional[datetime] = None) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown reason: {reason!r}")
        self.reason = reason
        self.locked_until = _iso(locked_until) if reason == TIMED_LOCK and locked_until is not None else None
        super().__init__(status_code=423, detail=_text(reason, self.locked_until))

    def body(self) -> Dict[str, Any]:
        return {
            "detail": self.detail,
            "reason": self.reason,
            "locked_until": self.locked_until,
            "lockout_minutes": lockout.LOCKOUT_MINUTES,
        }


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:  # SQLite hands a timezone-aware column back naive; the value is UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _text(reason: str, locked_until: Optional[str]) -> str:
    if reason == DEACTIVATED:
        return "This account is deactivated. An administrator must reactivate it."
    if reason == LOCK_WITHOUT_END:
        return "This account is locked. An administrator must unlock it."
    return (
        f"This account is locked after too many failed attempts. The lock lasts "
        f"{lockout.LOCKOUT_MINUTES} minutes and ends at {locked_until}; an administrator can unlock it earlier."
    )


def reason_for(row: Any) -> Optional[str]:
    """Why `row`'s account cannot be used right now, or None. Reads only."""
    if not row.is_active:
        return DEACTIVATED
    if not lockout.currently_locked(row):
        return None
    return TIMED_LOCK if getattr(row, "locked_until", None) is not None else LOCK_WITHOUT_END


def refusal(row: Any) -> AccountLocked:
    """The 423 for `row` as it is now. For an account that is in fact usable
    (the caller decided otherwise), a timed lock without a known end."""
    return AccountLocked(reason_for(row) or TIMED_LOCK, getattr(row, "locked_until", None))


async def account_locked_handler(_request: Request, exc: AccountLocked) -> JSONResponse:
    return JSONResponse(status_code=423, content=exc.body(), headers=getattr(exc, "headers", None))


def install(app: FastAPI) -> None:
    """Make `app` answer `AccountLocked` with its full body."""
    app.add_exception_handler(AccountLocked, account_locked_handler)
