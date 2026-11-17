# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Read model for the two per-request authentication look-ups.

Every admin request used to cost: a tenant `SELECT` in the middleware,
then a session `SELECT` and an admin `SELECT` in `verify_token`. Two of
those three answer a yes/no question ("is this session still active?",
"is this tenant still active and unsuspended?") whose answer changes
only on logout, revocation, expiry or a tenant status edit. Those
answers are kept here for a few seconds so a burst of console requests —
Mission Control polling, a log viewer, a form save fanning out into a
dozen GETs — pays for the query once.

This is a *read model*, not a cache of ORM objects: only the facts a
caller needs to decide are stored (an expiry, a name), so nothing
detached from a closed session ever reaches a handler. The admin row
itself is still loaded on every request; handlers use it.

Invalidation is explicit and lives at the choke points: the session
models' ``deactivate()`` (logout, admin revocation, console revocation,
expiry all go through it) and an ORM event on ``Tenant`` updates and
deletes. With several Gunicorn workers each holds its own copy, so a
revocation reaches the other workers within ``TTL_SECONDS`` at worst;
the core proxy's own session check has the same shape with a 30-second
window.
"""


import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

#: How long a positive answer is reused before the table is consulted again.
TTL_SECONDS = 5.0


class _TtlMap:
    """A tiny thread-safe map of key → (deadline, value)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: Dict[str, Tuple[float, object]] = {}

    def get(self, key: str):
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            deadline, value = entry
            if deadline < now:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value: object, ttl: float = TTL_SECONDS) -> None:
        with self._lock:
            # Bound the map so a credential-stuffing burst cannot grow it
            # without limit; entries expire on their own anyway.
            if len(self._items) > 50_000:
                self._items.clear()
            self._items[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_sessions = _TtlMap()
_tenants = _TtlMap()


# ── Admin sessions ──────────────────────────────────────────────────────


def session_valid_until(session_id: str) -> Optional[datetime]:
    """The cached ``expires_at`` of an active session, or ``None`` when the
    table has to be consulted."""
    value = _sessions.get(session_id)
    return value if isinstance(value, datetime) else None


def remember_session(session_id: str, expires_at: datetime) -> None:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    _sessions.put(session_id, expires_at)


def forget_session(session_id: Optional[str]) -> None:
    """Called from the session models' ``deactivate()``; a revoked session
    must be refused on the very next request in this worker."""
    if session_id:
        _sessions.invalidate(session_id)


# ── Tenants ─────────────────────────────────────────────────────────────


def tenant_known_active(tenant_id: str) -> Optional[str]:
    """The tenant's name when it was recently seen active and unsuspended,
    else ``None`` (consult the table)."""
    value = _tenants.get(tenant_id)
    return value if isinstance(value, str) else None


def remember_tenant_active(tenant_id: str, name: str) -> None:
    _tenants.put(tenant_id, name or "")


def forget_tenant(tenant_id: Optional[str]) -> None:
    if tenant_id:
        _tenants.invalidate(tenant_id)


def clear_all() -> None:
    """Test hook."""
    _sessions.clear()
    _tenants.clear()
