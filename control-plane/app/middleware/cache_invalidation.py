# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tell the caller when a config write landed but the proxies were not told.

`invalidate_proxy_cache` publishes *after* the transaction commits (see
`CommitBeforeResponseMiddleware` below for how that is arranged), so a
publisher outage cannot be undone by failing the request — the change is
already in the database. What it must not do is come back looking like an
unqualified success: the operator would walk away believing the new
guardrail, model or group binding is live when running proxies are still
serving the previous config out of cache.

There are ~120 call sites and none of them bind the return value, so the
signal is collected on the request instead (see
`app/services/cache_invalidation.py::record_invalidation_failure`) and
stamped onto whatever response the handler produced. That keeps the fix in
one place rather than in every handler, and it works for the 201/204/200
shapes alike.

Written as pure ASGI so the headers can be attached to the
`http.response.start` message; `BaseHTTPMiddleware` would buffer the
response body for no reason here.
"""

from typing import List

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.cache_invalidation import (
    DEFERRAL_STATE_KEY,
    FAILURE_STATE_KEY,
    commit_before_response,
    publish_committed,
)

# RFC 9111 §5.5 warn-code 199 "Miscellaneous warning" is the registered way
# to attach a non-fatal advisory to an otherwise successful response.
_WARNING_VALUE = (
    '199 brutor-gateway-control "Change committed, but proxy cache '
    'invalidation could not be published; proxies may continue to enforce '
    'the previous configuration until their cache entries expire"'
)


class CacheInvalidationWarningMiddleware:
    """Stamp a warning on responses whose invalidation publish failed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The endpoint and this middleware do not share a context, but they
        # do share the scope — so the slot has to live there.
        state = scope.setdefault("state", {})

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                failures: List[dict] = state.get(FAILURE_STATE_KEY) or []
                if failures:
                    headers = message.setdefault("headers", [])
                    headers.append((b"x-brutor-cache-invalidated", b"false"))
                    headers.append((b"warning", _WARNING_VALUE.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CommitBeforeResponseMiddleware:
    """Commit the request's writes before the response starts, then publish
    the proxy-cache invalidations they queued.

    `get_db` commits only in its teardown, which in the FastAPI in use runs
    after the response has been SENT: a commit failure there reached the
    client as a 200 for a change that was never saved, and a failed
    invalidation publish was too late for `CacheInvalidationWarningMiddleware`
    to warn about. So when the response starts, a request-bound session
    whose transaction holds uncommitted writes is committed (in the
    threadpool); a failure propagates before any header is sent, so `get_db`
    rolls back and the request ends in a 500 with nothing published. Then
    the invalidations queued by `invalidate_proxy_cache` are published and
    the headers go out. Requests without writes are untouched; `get_db`'s
    own commit stays as the backstop and its rollback-on-exception is
    unchanged. For a `StreamingResponse` the commit happens before the
    first byte; writes made *inside* the body iterator are committed by the
    backstop after the response, as before.

    Must be the INNERMOST middleware: here the send path is the router's
    own, so the handler and its dependencies are done with the session. A
    `BaseHTTPMiddleware` in between would run the inner app — including
    `get_db`'s teardown — in another task, concurrently with this send.

    Anything committed after the response started (backstop commit, error
    paths) is published once the inner app returns; its failure still
    raises the alert, but can no longer change the headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})[DEFERRAL_STATE_KEY] = True

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                await commit_before_response(scope)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            await publish_committed(scope)
