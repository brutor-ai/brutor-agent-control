# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Run an awaitable from a threadpool route handler.

Route handlers that hold a synchronous SQLAlchemy ``Session`` are plain
``def`` functions, so FastAPI runs them in the anyio threadpool and a
slow query blocks one worker thread instead of the whole event loop.
A few of those handlers also have to do something genuinely
asynchronous — publish a cache invalidation on the async Redis client,
probe an MCP server, read a multipart upload. ``block_on`` hands that
awaitable to the event loop the handler was dispatched from and waits
for the result, so the handler stays synchronous, the query stays off
the loop, and the async I/O still runs where its client lives.

The hop is cheap (a future round-trip) and preserves context variables
— anyio copies the calling thread's context onto the loop task, so the
tenant context, the authorship principal and request-scoped state all
carry across.

Outside a threadpool handler — a plain script, a synchronous unit test
— there is no loop to hand the awaitable to, so it is run to completion
on a private loop. Calling ``block_on`` *on* the event loop thread is a
programming error (it would deadlock) and is refused loudly; the ratchet
in ``tests/test_async_handler_ratchet.py`` keeps it out of ``async def``
handlers.
"""

import asyncio
import inspect
from typing import Any, Awaitable, Callable, TypeVar

from anyio import from_thread

T = TypeVar("T")

__all__ = ["block_on", "block_on_all"]


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run(factory: Callable[[], Awaitable[T]]) -> T:
    """Evaluate ``factory`` on the event loop and wait for its awaitable."""
    try:
        return from_thread.run(factory)  # type: ignore[arg-type]
    except RuntimeError as exc:
        if "AnyIO worker thread" not in str(exc):
            raise
    # Not inside a threadpool handler.
    if _on_event_loop_thread():
        raise RuntimeError(
            "block_on() was called on the event loop thread; an `async def` "
            "must await the coroutine directly instead"
        )

    async def _main() -> T:
        return await factory()

    return asyncio.run(_main())


def block_on(awaitable: Awaitable[T]) -> T:
    """Wait for ``awaitable`` from a synchronous route handler.

    Accepts a coroutine or any other awaitable; a non-awaitable (a mocked
    service returning a plain value) is passed straight through, which
    keeps ``MagicMock``-patched services working in tests.
    """
    if not inspect.isawaitable(awaitable):
        return awaitable  # type: ignore[return-value]
    return _run(lambda: awaitable)


def block_on_all(*awaitables: Awaitable[Any], return_exceptions: bool = False) -> list:
    """``asyncio.gather`` for a synchronous handler.

    ``gather`` must be *called* on the loop — it schedules its arguments
    as tasks — so the call is deferred into the factory rather than made
    in the worker thread.
    """
    if not awaitables:
        return []
    return list(
        _run(lambda: asyncio.gather(*awaitables, return_exceptions=return_exceptions))
    )
