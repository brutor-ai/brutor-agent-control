# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Drain mode reaches requests that were already in flight.

Drain tags responses `Connection: close` so a client's next request opens a
fresh connection the load balancer can send to a healthy replica. The mode
was decided only when a request ARRIVED: a request already running when an
operator switched drain on came back without the header, its keep-alive
socket stayed on the draining replica, and the client's next write there was
refused with 503 where a fresh connection would have succeeded
(`tests/load_test_drain_mode.py` caught it; it is not collected by default,
so it had been failing unnoticed). The mode is now re-read when the response
goes out.

Fast and deterministic: one request, drain switched on while it runs.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from app.middleware.maintenance import MODE_DRAIN, MODE_OFF, MODE_READ_ONLY, MaintenanceMiddleware

TENANT = "tenant-drain-in-flight"
_STATE = {"mode": MODE_OFF, "drain_grace_secs": 5}


class _StubSession:
    class _Query:
        def filter(self, *_a, **_k):
            return self

        def first(self):
            return (_STATE["mode"], _STATE["drain_grace_secs"])

    def query(self, *_a):
        return _StubSession._Query()

    def close(self):
        pass


def _app(started: asyncio.Event, release: asyncio.Event) -> FastAPI:
    app = FastAPI()
    # TTL 0: every lookup reads the current state (the NOTIFY consumer drops
    # the cache on a real toggle; this stands in for that).
    app.add_middleware(MaintenanceMiddleware, cache_ttl_secs=0.0, db_factory=lambda: _StubSession())

    class _Tenant(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant_id = TENANT
            return await call_next(request)

    app.add_middleware(_Tenant)

    @app.get("/v1/proxy/slow")
    async def slow() -> dict:
        started.set()
        await release.wait()
        return {"ok": True}

    return app


async def _in_flight_when(mode_during: str, mode_before: str = MODE_OFF) -> httpx.Response:
    _STATE["mode"] = mode_before
    started, release = asyncio.Event(), asyncio.Event()
    transport = httpx.ASGITransport(app=_app(started, release))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.get("/v1/proxy/slow"))
        await started.wait()
        _STATE["mode"] = mode_during  # operator switches mode mid-request
        release.set()
        return await task


@pytest.mark.asyncio
async def test_a_request_in_flight_when_drain_starts_is_told_to_close():
    resp = await _in_flight_when(MODE_DRAIN)
    assert resp.status_code == 200, "drain never aborts an in-flight request"
    assert resp.headers.get("connection", "").lower() == "close"


@pytest.mark.asyncio
async def test_read_only_turning_into_drain_also_closes():
    resp = await _in_flight_when(MODE_DRAIN, mode_before=MODE_READ_ONLY)
    assert resp.status_code == 200
    assert resp.headers.get("connection", "").lower() == "close"


@pytest.mark.asyncio
async def test_no_close_when_drain_did_not_start():
    resp = await _in_flight_when(MODE_OFF)
    assert resp.status_code == 200
    assert resp.headers.get("connection", "").lower() != "close"
