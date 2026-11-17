# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Opens the per-request slot that carries the acting admin.

The admin dependency binds the actor, and the flush listener reads it to
stamp `created_by` / `updated_by`. Between those two points sits a
threadpool: `get_current_admin` is a sync function, so FastAPI runs it via
`run_in_threadpool`, which hands it a **copy** of the context. A
`ContextVar.set` in a copy dies with the copy.

That is why the binding needs somewhere to write that the rest of the
request can see. This middleware installs a mutable slot in the request's
own context, before FastAPI resolves any dependency. Every context copy
made afterwards shares that slot *object*, so the dependency mutating it is
visible to the endpoint and to the flush — while the slot itself is created
fresh per request, which is what keeps two concurrent operators from being
recorded as each other.

Written as pure ASGI rather than `BaseHTTPMiddleware` deliberately: the
latter runs `dispatch` in its own task, and the whole point here is not to
put another context boundary in the way of the thing we are fixing.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.database.models.resource_mixins import open_actor_scope


class AuthorshipContextMiddleware:
    """Give each request a slot for the acting admin."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only HTTP requests carry an admin. Lifespan and websocket scopes
        # have no actor to record, and opening a slot for them would just
        # be noise.
        if scope["type"] == "http":
            open_actor_scope()
        await self.app(scope, receive, send)
