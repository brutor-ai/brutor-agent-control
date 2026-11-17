# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Maintenance-mode middleware.

The middleware is the single gate that turns a `tenants.maintenance_mode`
flag into observable 503 behaviour for callers. Four modes, each with a
narrow contract:

  * ``off``       — passthrough. The hot-path cost must be one cache read +
                    one string compare per request.
  * ``read_only`` — POST/PATCH/PUT/DELETE rejected with 503; GET/HEAD/OPTIONS
                    allowed. Used during config-export windows and
                    migration prep.
  * ``drain``     — same body as ``read_only`` PLUS ``Connection: close``
                    on every response so HTTP/1.1 keep-alive doesn't
                    hold the replica open past the drain grace window.
                    The operator-driven rolling restart watches replicas
                    finish their in-flight work and exit cleanly.
  * ``paused``    — every request rejected with 503. Used in incident
                    response when a tenant must be cut off from all
                    surfaces.

Two invariants the middleware must preserve to be safe in production:

  1. **The maintenance endpoint itself is always reachable.** If an
     operator flips ``paused`` by mistake and the maintenance endpoint
     was also gated, the only way back is direct DB access — which is
     fine in theory but a 30-minute outage in practice. The carve-out
     for ``/v1/admin/system/maintenance`` (and the health endpoints
     TenantMiddleware already exempts) is what keeps the recovery path
     manual-toggleable.

  2. **Tenant resolution must have already happened.** TenantMiddleware
     populates ``request.state.tenant_id``; this middleware reads it.
     The middleware order in app.py is therefore CORS → Tenant →
     Maintenance — register Maintenance AFTER Tenant.

Cache discipline:

  * Per-tenant TTL cache (1s). The proxy reads on every request; we
    cannot pay a DB hit per request just to learn the mode. 1s is short
    enough that a maintenance toggle propagates within the same second
    even without NOTIFY, and the NOTIFY consumer invalidates
    the entry immediately on receipt.
  * Negative cache: a missing tenant row caches as ``off`` for the same
    1s window — we don't want a broken DB to suddenly start blocking
    every request. If the row genuinely doesn't exist, TenantMiddleware
    would have already 404'd; reaching this middleware implies the
    tenant existed at lookup time.

Tests pass a synthesised ``Request`` with the right ``state.tenant_id``
already populated and a stub DB session; the cache TTL is overridden via
the ``cache_ttl_secs`` constructor arg so individual tests can force a
miss without sleeping.
"""

import time
from threading import RLock
from typing import Callable, Dict, NamedTuple, Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.tenant import Tenant


logger = BrutorLogger("maintenance_middleware")


# Methods considered "writes" for read_only / drain mode. The set is
# canonical — extending it would break the read_only contract for
# routes that happen to use TRACE / CONNECT, which we don't serve
# anyway. Kept frozenset for O(1) membership tests on the hot path.
WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


# Modes — duplicated as constants so the middleware doesn't need to
# import the Pydantic Literal type. Stays in sync with
# `MaintenanceMode` in app/api/v1/models/admin/system_ops.py — the
# regression test in test_system_ops_maintenance.py asserts the union.
MODE_OFF = "off"
MODE_READ_ONLY = "read_only"
MODE_DRAIN = "drain"
MODE_PAUSED = "paused"
VALID_MODES = (MODE_OFF, MODE_READ_ONLY, MODE_DRAIN, MODE_PAUSED)


# Path prefixes the middleware always allows through, regardless of
# mode. Health endpoints stay reachable so Docker / ECS health checks
# don't flap when an operator toggles paused; the maintenance
# endpoint itself stays reachable so an operator can revert a
# mistaken toggle without hitting the DB.
ALWAYS_ALLOWED_PREFIXES = (
    "/health",
    "/healthz",
    # The System Operations Maintenance endpoints. ALL of these must
    # be reachable when paused — GET so the UI can show current state,
    # PATCH so the operator can flip back to off, POST so emergency
    # restart-rolling / db-vacuum still work.
    "/v1/admin/system/maintenance",
)


class _CachedMode(NamedTuple):
    mode: str
    drain_grace_secs: Optional[int]
    expires_at: float


class MaintenanceMiddleware(BaseHTTPMiddleware):
    """Translates ``tenants.maintenance_mode`` into 503 responses.

    The middleware runs AFTER TenantMiddleware. It reads
    ``request.state.tenant_id`` (populated by Tenant) and looks up
    the per-tenant maintenance state from the DB, behind a tiny
    in-process TTL cache.

    Constructor args:
        cache_ttl_secs: TTL for the per-tenant lookup. Defaults to 1s.
            Tests can pass 0 to force a miss on every call.
        db_factory: Hook for tests. Production passes ``None`` and the
            middleware uses the shared ``get_db()`` generator the same
            way ``TenantMiddleware`` does.
    """

    def __init__(
        self,
        app,
        cache_ttl_secs: float = 1.0,
        db_factory: Optional[Callable[[], Session]] = None,
    ) -> None:
        super().__init__(app)
        self._cache_ttl = cache_ttl_secs
        # The lock guards the dict; lookups are short so RLock is
        # cheap and the hot path is dominated by the DB read on a
        # cold cache miss anyway.
        self._cache_lock = RLock()
        self._cache: Dict[str, _CachedMode] = {}
        # `db_factory` exists for tests that want to inject a stub
        # session without touching the real connection pool. Prod
        # always falls back to the normal `get_db()` generator —
        # same pattern TenantMiddleware uses, including the
        # try/finally close discipline.
        self._db_factory = db_factory

    # The NOTIFY consumer calls this to drop the cached
    # row for a tenant; the next request re-reads from DB.
    def invalidate(self, tenant_id: Optional[str]) -> None:
        """Drop the cached maintenance entry for one tenant (or all).

        Called from the ``brutor:maintenance`` NOTIFY consumer so a
        toggle on another replica propagates within milliseconds
        instead of waiting up to a full TTL. ``tenant_id=None`` clears
        every cached entry — used when the NOTIFY payload's
        ``tenant_id`` field is null (global change) or when the
        operator hits the "flush cache" debug endpoint.
        """
        with self._cache_lock:
            if tenant_id is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant_id, None)

    async def dispatch(self, request: Request, call_next):
        # Always-allowed paths bypass before any state lookup so a
        # /health probe doesn't pay the lock + cache read on every hit.
        path = request.url.path
        if self._is_always_allowed(path):
            return await call_next(request)

        # Pull the tenant from request.state. TenantMiddleware
        # populates this; if it isn't there we have nothing to gate on
        # (root-level / system-admin login flows). Let the request
        # through — the absence of tenant context means the request
        # already passed the Tenant exclusion list.
        tenant_id: Optional[str] = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            return await call_next(request)

        # Look up the mode. The cold-miss path opens a DB session via
        # the same `get_db()` generator TenantMiddleware uses so the
        # connection pool sizing is consistent.
        mode, drain_grace_secs = self._lookup(tenant_id)

        # Off — passthrough. This is the dominant case in production;
        # the lookup above (plus the cached re-read below) is the entire
        # hot-path cost.
        if mode == MODE_OFF:
            response = await call_next(request)
            self._close_if_drain_started(tenant_id, response)
            return response

        # Paused — every request 503s. Operator runbook expects this
        # to be the rare incident-response toggle, NOT a routine
        # maintenance window — that's what read_only / drain are for.
        if mode == MODE_PAUSED:
            return self._maintenance_response(
                mode=MODE_PAUSED,
                detail=(
                    "Tenant is in 'paused' maintenance mode. All requests are "
                    "rejected until an operator toggles it off."
                ),
                attach_close=False,
            )

        # Read-only and drain — block writes only. GET / HEAD /
        # OPTIONS pass through. Drain additionally tags the response
        # with `Connection: close` so HTTP/1.1 keep-alive doesn't
        # hold the replica past the grace window.
        if mode in (MODE_READ_ONLY, MODE_DRAIN):
            if request.method in WRITE_METHODS:
                detail = (
                    f"Tenant is in '{mode}' maintenance mode. "
                    f"{request.method} is rejected; reads remain available."
                )
                return self._maintenance_response(
                    mode=mode,
                    detail=detail,
                    attach_close=(mode == MODE_DRAIN),
                    retry_after_secs=drain_grace_secs if mode == MODE_DRAIN else None,
                )
            # Read passes through; if drain, signal the client to
            # close the keep-alive socket after the response.
            response = await call_next(request)
            if mode == MODE_DRAIN:
                response.headers["Connection"] = "close"
            else:
                self._close_if_drain_started(tenant_id, response)
            return response

        # Unknown mode — log loudly and fail open. A misconfigured
        # mode string (set out-of-band on the DB) shouldn't take the
        # whole tenant offline; operators get a chance to fix the row
        # with the request surface still alive.
        logger.warning(
            "Unknown maintenance_mode value; treating as off",
            context={"tenant_id": tenant_id, "mode": mode},
        )
        return await call_next(request)

    def _maintenance_response(
        self,
        mode: str,
        detail: str,
        attach_close: bool,
        retry_after_secs: Optional[int] = None,
    ) -> Response:
        """Build the canonical 503 response.

        Response headers:
            * ``X-Brutor-Maintenance-Mode``: machine-readable mode token.
              The portal / admin UI surface this in a banner so the
              end-user sees "the tenant is read-only" rather than a
              generic 503.
            * ``Connection: close`` when drain — tells HTTP/1.1 keep-
              alive clients not to reuse the socket.
            * ``Retry-After``: drain-grace-secs when available, so a
              well-behaved client can pace its retry within the
              announced grace window.
        """
        headers = {
            "X-Brutor-Maintenance-Mode": mode,
        }
        if attach_close:
            headers["Connection"] = "close"
        if retry_after_secs is not None and retry_after_secs > 0:
            headers["Retry-After"] = str(retry_after_secs)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": detail, "maintenance_mode": mode},
            headers=headers,
        )

    def _is_always_allowed(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in ALWAYS_ALLOWED_PREFIXES)

    def _close_if_drain_started(self, tenant_id: str, response: Response) -> None:
        """Tag `Connection: close` when drain began while the request ran.

        The mode is decided when a request arrives. A request already in
        flight when an operator switched drain on otherwise came back
        without the header, kept its keep-alive socket on the draining
        replica, and the client's next write there was refused (503) where
        a fresh connection would have reached a healthy one
        (`tests/test_maintenance_drain_in_flight.py`). The re-read goes
        through the same TTL cache as the arrival lookup, which the
        maintenance NOTIFY consumer drops on a toggle.
        """
        mode, _ = self._lookup(tenant_id)
        if mode == MODE_DRAIN:
            response.headers["Connection"] = "close"

    def _lookup(self, tenant_id: str) -> tuple[str, Optional[int]]:
        """Return (mode, drain_grace_secs) for the tenant, behind a TTL cache.

        DB error path: log and return MODE_OFF. Failing open is
        deliberate — taking the whole tenant offline because the DB
        had a transient blip is strictly worse than letting requests
        through. A real DB outage would also bring down the rest of
        the request anyway, so this branch can't actually mask a
        meaningful failure mode.
        """
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(tenant_id)
            if entry and entry.expires_at > now:
                return entry.mode, entry.drain_grace_secs

        # Cache miss — open a session, read the row, cache the answer.
        # Same try/finally close discipline as TenantMiddleware.
        db_iter = None
        try:
            if self._db_factory is not None:
                db = self._db_factory()
            else:
                db_iter = get_db()
                db = next(db_iter)
            row = (
                db.query(Tenant.maintenance_mode, Tenant.drain_grace_secs)
                .filter(Tenant.id == tenant_id)
                .first()
            )
            if row is None:
                # No row — fail open. The Tenant middleware would
                # already have 404'd if the tenant didn't exist, so
                # we shouldn't usually hit this branch.
                mode = MODE_OFF
                drain = None
            else:
                mode = row[0] or MODE_OFF
                drain = row[1]
            entry = _CachedMode(
                mode=mode,
                drain_grace_secs=drain,
                expires_at=now + self._cache_ttl,
            )
            with self._cache_lock:
                self._cache[tenant_id] = entry
            return mode, drain
        except Exception as exc:
            logger.warning(
                "Maintenance lookup failed; failing open",
                context={"tenant_id": tenant_id, "error": str(exc)},
            )
            return MODE_OFF, None
        finally:
            if db_iter is not None:
                # Drain the generator so `get_db()`'s finally clause
                # runs and the session returns to the pool.
                try:
                    next(db_iter, None)
                except Exception:
                    pass
