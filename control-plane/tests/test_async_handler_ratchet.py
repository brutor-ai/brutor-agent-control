# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Ratchet: route handlers that hold a synchronous SQLAlchemy ``Session`` must
not be ``async def`` unless they genuinely await something.

FastAPI runs an ``async def`` handler on the event loop; a synchronous
query inside it blocks every other request served by that worker for the
duration of the query. With two Uvicorn workers that made the admin API's
effective concurrency for anything touching the database two requests.
A plain ``def`` handler runs in the threadpool instead (contextvars —
tenant context, authorship — are propagated by anyio).

Three things are pinned:

* handlers with a ``Session`` parameter that are ``async`` *and never
  await* — the pure regression: **must be zero**;
* handlers with a ``Session`` parameter that are ``async`` because they
  also await something — **also zero**: the awaitable is
  handed to ``app.core.sync_bridge.block_on`` (or ``block_on_all`` for a
  fan-out) from a plain ``def`` handler, so the query runs in the
  threadpool and the I/O still runs on the loop. The ceiling constant
  stays so a regression reads as "the count went up", not "a test broke";
* ``block_on`` is never called from inside an ``async def`` anywhere in
  ``app/`` — on the loop thread it would deadlock, and the bridge refuses
  it at runtime; this catches it at review time.
"""

import ast
import glob
import os
import re

import pathlib

from tests.source_trees import python_sources

APP = os.path.join(os.path.dirname(__file__), "..", "app")
ROOT = os.path.join(APP, "api", "v1", "endpoints")

# Debt ceiling for async handlers that hold a Session AND await. Lower it
# as handlers are converted; never raise it.
MAX_ASYNC_SESSION_HANDLERS_THAT_AWAIT = 0


def _route_handlers():
    for f in map(str, python_sources(pathlib.Path(ROOT))):
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in ("get", "post", "put", "patch", "delete")
                for d in node.decorator_list
            )
            if not is_route:
                continue
            has_session = any(
                a.annotation is not None and "Session" in ast.unparse(a.annotation)
                for a in node.args.args + node.args.kwonlyargs
            )
            if not has_session:
                continue
            body = ast.unparse(node)
            awaits = bool(re.search(r"\bawait\b|async with|async for", body))
            yield os.path.relpath(f, ROOT), node.name, awaits


def test_no_async_handler_holds_a_session_without_awaiting():
    offenders = [(f, n) for f, n, awaits in _route_handlers() if not awaits]
    assert offenders == [], (
        "These handlers are `async def` but never await, and they hold a sync "
        "Session: make them `def` so the query runs in the threadpool: "
        f"{offenders}"
    )


def test_async_handlers_that_hold_a_session_and_await_only_go_down():
    count = sum(1 for _, _, awaits in _route_handlers() if awaits)
    assert count <= MAX_ASYNC_SESSION_HANDLERS_THAT_AWAIT, (
        f"{count} async handlers hold a sync Session while awaiting; the "
        f"ceiling is {MAX_ASYNC_SESSION_HANDLERS_THAT_AWAIT}. Do not add more."
    )
    # Keep the ceiling honest: if it is now well above reality, lower it.
    assert MAX_ASYNC_SESSION_HANDLERS_THAT_AWAIT - count <= 10, (
        f"only {count} remain; lower MAX_ASYNC_SESSION_HANDLERS_THAT_AWAIT to {count}"
    )


def test_block_on_is_never_called_from_an_async_function():
    """``block_on`` blocks the calling thread until the loop finishes the
    awaitable. From an ``async def`` that thread *is* the loop."""
    offenders = []
    for f in map(str, python_sources(pathlib.Path(APP))):
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        if "block_on" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in ("block_on", "block_on_all")
                ):
                    offenders.append((os.path.relpath(f, APP), node.name))
    assert offenders == [], f"block_on() inside async def: {offenders}"
