# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The route x principal matrix.

Every admin route an application mounts is asked by every kind of caller, and
the status of each answer is the matrix:

* `anonymous`: no token;
* `tenant`: the full-permission administrator of tenant `t1`;
* `tenant_other`: the same administrator naming another tenant
  (`?tenant_id=t2`);
* `system`: the system administrator, naming no tenant;
* `system_named`: the system administrator naming tenant `t1`.

Path parameters are `nope` and the body is `{}`: the matrix records who is let
in, not what a route does. Outgoing connections are refused, so a route that
would call another service answers the same everywhere.

`sweep` is used twice: against the composition that runs today, where the
result is compared with the recording (`fixtures/admin_principal_matrix.json`,
made on the code before the principal registry existed), and against the
Community composition, where the result is the new contract.
"""

from __future__ import annotations

import re
import socket
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from fastapi.testclient import TestClient

PREFIXES = ("/v1/admin/", "/v1/admin/users/")
COLUMNS = ("anonymous", "tenant", "tenant_other", "system", "system_named")
LOGOUT = ("POST", "/v1/admin/users/logout")

Route = Tuple[str, str]


def admin_routes(app) -> List[Route]:
    """Every admin operation of `app`, logout last (it ends the session the
    other answers are asked with)."""
    routes = sorted(
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(PREFIXES)
        for method in item
    )
    if LOGOUT in routes:
        routes.remove(LOGOUT)
        routes.append(LOGOUT)
    return routes


def url(path: str, tenant: Optional[str] = None) -> str:
    out = re.sub(r"\{(\w+)(:\w+)?\}", "nope", path)
    return f"{out}?tenant_id={tenant}" if tenant else out


def key(route: Route) -> str:
    return f"{route[0]} {route[1]}"


class no_network:
    """Refuse every outgoing connection while the matrix is asked."""

    def __enter__(self):
        self._connect = socket.socket.connect
        self._connect_ex = socket.socket.connect_ex
        self._getaddrinfo = socket.getaddrinfo

        def refuse(*_a, **_k):
            raise ConnectionRefusedError("the principal matrix makes no connections")

        socket.socket.connect = refuse
        socket.socket.connect_ex = refuse
        socket.getaddrinfo = refuse
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._connect
        socket.socket.connect_ex = self._connect_ex
        socket.getaddrinfo = self._getaddrinfo
        return False


class no_leftovers:
    """Process-wide state another test may have left, which a route would
    answer from instead of from its (refused) connection. One today: the
    A2A registry catalog's in-memory cache (`GET /agent-cards/catalog`
    answers 200 from it, 502 without). Emptied for the sweep, put back after."""

    def __enter__(self):
        from app.api.v1.endpoints.admin import agent_cards

        self._cache = agent_cards._REGISTRY_CACHE
        self._saved = dict(self._cache)
        self._cache.clear()
        return self

    def __exit__(self, *exc):
        self._cache.clear()
        self._cache.update(self._saved)
        return False


def sweep(
    app,
    routes: Iterable[Route],
    headers: Dict[str, Optional[Callable[[], Dict[str, str]]]],
    after_column: Callable[[], None] = lambda: None,
) -> Dict[str, Dict[str, int]]:
    """Ask every route as every column of `headers`; `{route: {column: status}}`.

    `headers[column]` returns the bearer headers of that caller, or is None for
    a caller that cannot be made (no such principal): its column is left out.
    Column by column, so that what one caller changed is the same for every
    route the next caller asks. `after_column` runs after each: the last route
    of a column is the logout, so the next caller has to sign in again.
    """
    client = TestClient(app, raise_server_exceptions=False)
    out: Dict[str, Dict[str, int]] = {key(route): {} for route in routes}
    named = {"tenant_other": "t2", "system_named": "t1"}
    with no_network(), no_leftovers():
        for column in COLUMNS:
            make = headers.get(column, lambda: {})
            if make is None:
                continue
            for route in routes:
                method, path = route
                response = client.request(method, url(path, named.get(column)), json={}, headers=make())
                out[key(route)][column] = response.status_code
            after_column()
    return out


from tests.community_process import keep_enterprise_out, let_enterprise_in  # noqa: E402


class community_principals:
    """The principal registry of the Community composition, for a `with`
    block: the process-wide registry without the entries tagged with an
    Enterprise Edition feature. This build composes both editions, so the
    Community composition has to be asked for."""

    def __enter__(self):
        from app.composition.principals import PRINCIPALS

        self._registry = PRINCIPALS
        self._saved = PRINCIPALS._specs
        keep_enterprise_out(PRINCIPALS)
        PRINCIPALS._specs = {name: spec for name, spec in self._saved.items() if spec.feature is None}
        return PRINCIPALS

    def __exit__(self, *exc):
        self._registry._specs = self._saved
        let_enterprise_in(self._registry)
        return False


class community_hooks:
    """The behaviour hooks of the Community composition, for a
    `with` block: the process-wide registry without the entries tagged with
    an Enterprise Edition feature."""

    def __enter__(self):
        from app.composition.hooks import HOOKS

        self._registry = HOOKS
        self._saved = HOOKS._specs
        keep_enterprise_out(HOOKS)
        HOOKS._specs = {name: spec for name, spec in self._saved.items() if spec.feature is None}
        return HOOKS

    def __exit__(self, *exc):
        self._registry._specs = self._saved
        let_enterprise_in(self._registry)
        return False


class community_identity_protocols:
    """The identity protocols of the Community composition, for
    a `with` block: the process-wide registry without the entries tagged with
    an Enterprise Edition feature."""

    def __enter__(self):
        from app.composition.identity import IDENTITY_PROTOCOLS

        self._registry = IDENTITY_PROTOCOLS
        self._saved = IDENTITY_PROTOCOLS._specs
        keep_enterprise_out(IDENTITY_PROTOCOLS)
        IDENTITY_PROTOCOLS._specs = {name: spec for name, spec in self._saved.items() if spec.feature is None}
        return IDENTITY_PROTOCOLS

    def __exit__(self, *exc):
        self._registry._specs = self._saved
        let_enterprise_in(self._registry)
        return False


class community_registries:
    """Every process-wide registry as the Community composition has it:
    principals, behaviour hooks, identity protocols."""

    def __enter__(self):
        self._parts = [community_principals(), community_hooks(), community_identity_protocols()]
        for part in self._parts:
            part.__enter__()
        return self

    def __exit__(self, *exc):
        for part in reversed(self._parts):
            part.__exit__(*exc)
        return False


def community_app(get_db_override):
    """The Community composition's application: the routers no Enterprise
    Edition feature owns, on a bare app (no middleware), with `get_db`
    overridden. Ask it inside `community_principals()`."""
    from fastapi import FastAPI

    from app.composition.routers import ROUTERS, mount_routers
    from app.composition.services import build_services
    from app.core.logging_system import setup_logging
    from app.database import get_db

    app = FastAPI()
    specs = [spec for spec in ROUTERS if spec.feature is None]
    mount_routers(app, build_services(setup_logging("INFO", use_json=False)), specs)
    app.dependency_overrides[get_db] = get_db_override
    return app
