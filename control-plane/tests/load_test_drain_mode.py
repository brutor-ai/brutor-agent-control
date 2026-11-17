# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Drain-mode behavioural load test.

What this asserts:

    * In drain mode, IN-FLIGHT GETs complete normally — drain does NOT
      kill existing traffic mid-flight. Operators rely on this when
      pulling a replica out of rotation for a deploy: existing
      streaming LLM responses keep flowing until the client closes the
      socket.
    * NEW POSTs that arrive after the drain toggle are rejected with
      503 and the `X-Brutor-Maintenance-Mode: drain` header.
    * NEW GETs that arrive after the drain toggle still succeed —
      drain is read-only for new traffic, not blanket-503.
    * The drain `Connection: close` header is present on every
      response served while drain is active.

Why this is a "load" test rather than a unit test:

    Unit tests cover the middleware decision logic with synthetic
    Requests. This file exercises the FULL Starlette/ASGI plumbing
    against the actual middleware stack — order matters (Tenant must
    run before Maintenance), the JSONResponse must serialise cleanly,
    and the body-close discipline must survive ASGI's
    response-streaming pipeline. Doing this with real concurrent
    requests catches integration issues a unit test can't see.

Expected wall-clock: ~5 seconds.

    Phase breakdown:
      * t=0    — fire N=20 concurrent slow GETs (each holds ~3s)
      * t=0.5s — toggle drain on
      * t=0.6s — fire 10 fast GETs + 10 fast POSTs (new traffic)
      * t=4s   — gather every result, assert per above

This file is `tests/load_test_*` rather than `tests/test_*` so the
default pytest collection picks it up only when the operator opts in
with `pytest tests/load_test_drain_mode.py` — it touches more
infrastructure than the typical unit and is gated by a real DB.

To run locally end-to-end through the Brutor gateway:

    # inside the gateway-control repo, with the local docker-compose
    # postgres up:
    pytest tests/load_test_drain_mode.py -q
"""

import asyncio
from typing import List, Tuple

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.middleware.maintenance import (
    MODE_DRAIN,
    MODE_OFF,
    MaintenanceMiddleware,
)


# Synthetic tenant id used by the test app. The real tenant table
# isn't required — the test app installs a stub DB factory that
# returns the desired mode directly. Same approach the middleware
# unit tests use, just at higher concurrency.
TEST_TENANT_ID = "tenant-drain-loadtest"


# In-process sentinel that the test mutates to flip mode. The
# middleware's db_factory reads from this so we can toggle drain on
# without a real DB roundtrip.
_MODE_STATE = {"mode": MODE_OFF, "drain_grace_secs": 5}


class _StubSession:
    """Minimal duck-type for the ``Session.query(...).filter(...).first()``
    chain the middleware uses. Returns ``(mode, drain_grace_secs)`` or
    ``None`` — same shape as the real SQLAlchemy result.
    """

    class _Query:
        def __init__(self, mode: str, drain: int):
            self._mode = mode
            self._drain = drain

        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return (self._mode, self._drain)

    def query(self, *_args):  # noqa: D401
        return _StubSession._Query(
            _MODE_STATE["mode"], _MODE_STATE["drain_grace_secs"]
        )

    def close(self):
        return None


def _stub_db_factory() -> Session:
    return _StubSession()  # type: ignore[return-value]


def _build_app() -> FastAPI:
    """FastAPI app with a slow GET + fast POST + the maintenance middleware.

    The slow GET sleeps 3s before returning — that gives us a window
    to toggle drain on while requests are in flight, then verify that
    those in-flight requests still complete cleanly.
    """
    app = FastAPI()

    # The middleware needs `request.state.tenant_id`; we set it via a
    # second middleware that runs FIRST (so it executes before
    # maintenance) — same ordering invariant the production
    # `app.add_middleware(MaintenanceMiddleware)` then
    # `app.add_middleware(TenantMiddleware)` enforces.
    from starlette.middleware.base import BaseHTTPMiddleware

    class _TenantStub(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant_id = TEST_TENANT_ID
            return await call_next(request)

    app.add_middleware(
        MaintenanceMiddleware, cache_ttl_secs=0.0, db_factory=_stub_db_factory
    )
    app.add_middleware(_TenantStub)

    @app.get("/v1/proxy/slow")
    async def slow_get() -> dict:
        await asyncio.sleep(3.0)
        return {"ok": True}

    @app.post("/v1/proxy/write")
    async def fast_write() -> dict:
        return {"written": True}

    @app.get("/v1/proxy/read")
    async def fast_read() -> dict:
        return {"read": True}

    return app


@pytest.mark.asyncio
async def test_drain_lets_inflight_complete_and_blocks_new_writes() -> None:
    _MODE_STATE["mode"] = MODE_OFF
    _MODE_STATE["drain_grace_secs"] = 5

    app = _build_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Step 1 — fire 20 slow GETs against /v1/proxy/slow. They'll
        # each sleep 3s on the server. We DON'T await them yet; we want
        # them running concurrently so we can toggle drain on while
        # they're in flight.
        in_flight = [
            asyncio.create_task(client.get("/v1/proxy/slow"))
            for _ in range(20)
        ]

        # Give the server time to actually enter the slow-handler.
        # 0.5s leaves headroom for ASGI startup + dispatch.
        await asyncio.sleep(0.5)

        # Step 2 — toggle drain on. The middleware's TTL cache was
        # initialised with ttl=0 so the next request reads the new
        # mode without waiting.
        _MODE_STATE["mode"] = MODE_DRAIN

        # Step 3 — fire NEW reads + writes after the toggle.
        new_reads = [
            asyncio.create_task(client.get("/v1/proxy/read"))
            for _ in range(10)
        ]
        new_writes = [
            asyncio.create_task(client.post("/v1/proxy/write"))
            for _ in range(10)
        ]

        # Wait for everything. The slow GETs take ~3s; total
        # wall-clock should be just under 4s with concurrency.
        results = await asyncio.gather(
            *in_flight, *new_reads, *new_writes, return_exceptions=True
        )

    in_flight_results = results[:20]
    new_read_results = results[20:30]
    new_write_results = results[30:]

    # ── Assert step 1: in-flight GETs complete with 200 ─────────────
    for r in in_flight_results:
        assert not isinstance(r, Exception), (
            f"in-flight GET raised — drain should not abort active requests: {r}"
        )
        assert r.status_code == 200, (
            f"in-flight GET should complete with 200, got {r.status_code}: "
            f"{r.text}"
        )
        # Drain mode tags every response with Connection: close.
        assert r.headers.get("connection", "").lower() == "close", (
            "in-flight response under drain mode must carry Connection: close"
        )

    # ── Assert step 3: NEW GETs succeed (drain is read-only) ────────
    for r in new_read_results:
        assert not isinstance(r, Exception), f"new GET raised: {r}"
        assert r.status_code == 200, (
            f"new GET under drain should return 200 (reads allowed), "
            f"got {r.status_code}"
        )
        assert r.headers.get("connection", "").lower() == "close"

    # ── Assert step 3: NEW POSTs rejected with 503 + drain header ──
    for r in new_write_results:
        assert not isinstance(r, Exception), f"new POST raised: {r}"
        assert r.status_code == 503, (
            f"new POST under drain should be 503, got {r.status_code}: "
            f"{r.text}"
        )
        assert r.headers.get("x-brutor-maintenance-mode") == "drain", (
            "503 response must advertise the maintenance mode for the "
            "portal banner / admin UI"
        )
        assert r.headers.get("connection", "").lower() == "close"
        # Retry-After must echo the drain_grace_secs so a well-
        # behaved client paces its retry within the announced window.
        assert r.headers.get("retry-after") == "5", (
            "drain mode must advertise drain_grace_secs as Retry-After"
        )
