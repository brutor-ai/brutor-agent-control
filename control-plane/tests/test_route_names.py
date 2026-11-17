# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The route names of the renamed admin surfaces.

Renamed without aliases or redirects: an old path is not served. Held on
the Community application here, and on the Enterprise one in
`enterprise/tests/test_route_names.py`.

| Row | Old | New |
|---|---|---|
| 13 | `/v1/admin/registry` | `/v1/admin/agent-registry` |
| 13 | `/v1/admin/registries` | `/v1/admin/mcp-registries` |
| 13 | `/v1/admin/mcp-registry` | `/v1/admin/mcp-registries/instance` |
| 14 | `/v1/admin/deploy/{docker,kubernetes}` | `/v1/admin/deployments/{docker,kubernetes}` |
| 14 | `/v1/admin/deployment/capabilities` | `/v1/admin/deployments/capabilities` |
| 15 | `/v1/admin-users` | `/v1/admin/users` |
| 17 | `/v1/admin/tool-approvals` | `/v1/admin/approvals` |
| 18 | `/v1/admin/notifications/{send,sent}` | `/v1/admin/user-notifications/{send,sent}` |

Row 16, `/v1/internal/governance`, keeps its path: it is already under
`/v1/internal`, the convention of the gateway's own system-caller route
(`/v1/internal/approvals`).
"""

from __future__ import annotations

import re

import pytest

from tests.app_factory import create_community_app

# A path that starts with one of these is an old name.
OLD = re.compile(
    r"^/v1/(?:"
    r"admin-users"
    r"|admin/registry"
    r"|admin/registries"
    r"|admin/mcp-registry"
    r"|admin/deploy"
    r"|admin/deployment"
    r"|admin/tool-approvals"
    r"|admin/notifications/(?:send|sent)"
    r")(?:/|$)"
)

COMMUNITY_NEW = {
    "/v1/admin/agent-registry",
    "/v1/admin/users/tenant/login",
    "/v1/admin/users/me",
    "/v1/admin/users/roles",
    "/v1/admin/users/audit-logs",
    "/v1/admin/approvals",
    "/v1/admin/approvals/pending-count",
    "/v1/admin/notifications/channels",
}

# What a client of the old names would call.
OLD_REQUESTS = [
    ("POST", "/v1/admin-users/tenant/login"),
    ("GET", "/v1/admin-users/me"),
    ("GET", "/v1/admin/registry"),
    ("GET", "/v1/admin/tool-approvals"),
    ("GET", "/v1/admin/tool-approvals/pending-count"),
]


def served_by(app, method: str, path: str) -> list:
    """The routes of `app` that answer `method path`, a redirect included:
    asked of the route table, so that no middleware answers first."""
    from fastapi.routing import iter_route_contexts
    from starlette.routing import compile_path

    found = []
    for context in iter_route_contexts(app.router.routes):
        regex, _format, _convertors = compile_path(context.path)
        if regex.match(path) and method in (context.methods or {method}):
            found.append(context.path)
    return found


def old_paths(paths) -> list:
    return sorted(p for p in paths if OLD.match(p))


@pytest.fixture(scope="module")
def app():
    return create_community_app()


def test_no_old_name_is_in_the_document(app):
    assert old_paths(app.openapi()["paths"]) == []


def test_the_new_names_are(app):
    assert COMMUNITY_NEW <= set(app.openapi()["paths"])


@pytest.mark.parametrize("method, path", OLD_REQUESTS)
def test_an_old_path_is_not_served_and_not_redirected(app, method, path):
    assert served_by(app, method, path) == []
