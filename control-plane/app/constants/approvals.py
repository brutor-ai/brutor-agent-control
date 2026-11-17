# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tool approval request status vocabulary — the control-plane mirror of
`brutor-gateway-core/src/routes/portal_approvals.rs::approval_status`.

Both planes read and write `tool_approval_requests.status`, so both must agree
on what each value means. `tests/test_wire_contract.py` pins the literals.

- `approved` and `consumed` both mean "a person said yes"; `consumed` only adds
  that the one-time token has been redeemed (set by the core when the agent
  retries). A filter for approved requests therefore selects both, or nearly
  every real approval would vanish from the Approved view.
- A `pending` row past its `expires_at` is effectively `expired`, whether or
  not the core's 60-second sweeper has rewritten it yet: it is not actionable
  and must not be counted as pending.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

PENDING = "pending"
APPROVED = "approved"
CONSUMED = "consumed"
REJECTED = "rejected"
EXPIRED = "expired"

#: Every status either plane can write.
ALL_STATUSES: Tuple[str, ...] = (PENDING, APPROVED, CONSUMED, REJECTED, EXPIRED)
#: "A person said yes."
APPROVED_STATUSES: Tuple[str, ...] = (APPROVED, CONSUMED)
#: "A person answered", yes or no. `consumed` belongs here: it is an approval
#: whose token the agent has since redeemed. A count of human decisions that
#: leaves it out loses every approval that was actually carried out (a
#: compliance oversight row once read "1 of 42" for that reason).
DECIDED_STATUSES: Tuple[str, ...] = (APPROVED, CONSUMED, REJECTED)

FILTER_ALL = "all"
#: The `status_filter` values the list endpoint accepts, in display order.
FILTERS: Tuple[str, ...] = (PENDING, APPROVED, CONSUMED, REJECTED, EXPIRED, FILTER_ALL)

UNKNOWN_DECISION = "unknown"


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_lapsed(status: str, expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """A pending request whose deadline has passed."""
    exp = _aware(expires_at)
    return status == PENDING and exp is not None and exp < (now or datetime.now(timezone.utc))


def decision(status: str, expires_at: Optional[datetime], now: Optional[datetime] = None) -> str:
    """The decision a person made, derived from the stored status."""
    if status == PENDING:
        return EXPIRED if is_lapsed(status, expires_at, now) else PENDING
    if status in APPROVED_STATUSES:
        return APPROVED
    if status in (REJECTED, EXPIRED):
        return status
    return UNKNOWN_DECISION


def is_decided(status: str) -> bool:
    """Whether a person answered the request (approved, approved and since
    redeemed, or rejected)."""
    return status in DECIDED_STATUSES


def token_used(status: str) -> bool:
    """Whether the one-time approval token has been redeemed."""
    return status == CONSUMED
