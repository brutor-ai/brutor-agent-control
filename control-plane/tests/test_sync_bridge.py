# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
``app.core.sync_bridge`` — the three places it can be called from.

* a threadpool route handler (the real case): the awaitable runs on the
  loop the request came in on, so a loop-bound client (async Redis, an
  ``httpx.AsyncClient``) works, and the caller's context variables are
  visible to the coroutine;
* a plain synchronous caller with no loop at all (scripts, unit tests):
  the awaitable is run to completion on a private loop;
* the event-loop thread itself: refused, because waiting there would
  deadlock the loop.
"""

import asyncio
import contextvars
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.sync_bridge import block_on, block_on_all

_who = contextvars.ContextVar("who", default="nobody")


async def _loop_thread_and_ctx():
    await asyncio.sleep(0)
    return threading.get_ident(), _who.get()


def test_from_a_threadpool_handler_the_awaitable_runs_on_the_request_loop():
    app = FastAPI()
    seen = {}

    @app.get("/loop")
    async def loop_ident():  # runs on the loop: records its thread id
        seen["loop_thread"] = threading.get_ident()
        return {}

    @app.get("/bridge")
    def sync_handler():  # threadpool: block_on must hop back to that loop
        _who.set("handler")
        handler_thread = threading.get_ident()
        loop_thread, ctx = block_on(_loop_thread_and_ctx())
        return {"same_thread": loop_thread == handler_thread, "loop_thread": loop_thread, "ctx": ctx}

    with TestClient(app) as client:
        client.get("/loop")
        body = client.get("/bridge").json()

    assert body["same_thread"] is False, "the handler ran in the threadpool"
    assert body["loop_thread"] == seen["loop_thread"], "the awaitable ran on the app's loop"
    assert body["ctx"] == "handler", "context variables set in the handler reach the coroutine"


def test_block_on_all_fans_out_and_keeps_order():
    app = FastAPI()

    async def _delayed(i):
        await asyncio.sleep(0.01 * (3 - i))
        return i

    @app.get("/all")
    def handler():
        return {"order": block_on_all(*(_delayed(i) for i in range(3)))}

    with TestClient(app) as client:
        assert client.get("/all").json()["order"] == [0, 1, 2]


def test_block_on_all_can_return_exceptions_in_place():
    async def _boom():
        raise ValueError("no")

    async def _ok():
        return "ok"

    out = block_on_all(_ok(), _boom(), return_exceptions=True)
    assert out[0] == "ok" and isinstance(out[1], ValueError)
    assert block_on_all() == []


def test_without_a_loop_the_awaitable_runs_on_a_private_loop():
    async def _seven():
        await asyncio.sleep(0)
        return 7

    assert block_on(_seven()) == 7


def test_non_awaitables_pass_straight_through():
    """A ``MagicMock``-patched service returns a plain value; the bridge must
    not try to await it."""
    assert block_on(42) == 42
    assert block_on({"a": 1}) == {"a": 1}


def test_calling_from_the_event_loop_thread_is_refused():
    async def _inner():
        async def _never():
            return 1

        coro = _never()
        try:
            with pytest.raises(RuntimeError, match="event loop thread"):
                block_on(coro)
        finally:
            coro.close()

    asyncio.run(_inner())
