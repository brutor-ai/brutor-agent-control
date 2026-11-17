# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The knowledge-base connector icons are the Enterprise Edition's.

Knowledge bases and their connectors are an Enterprise feature; the eight
brand icons of the connector catalog were still held by the Community
package (`app/assets/connector_icons/`) and served by a Community router.
The Community tree holds none of them and a Community application has no
route for them; the Enterprise Edition serves them at the same address
(`enterprise/tests/test_connector_icons.py`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app_factory import create_community_app

REPO = Path(__file__).resolve().parents[1]
COMMUNITY = REPO / "app"
CONNECTOR_TYPES = (
    "confluence", "notion", "google_drive", "slack", "github", "web_crawler", "jira", "sharepoint",
)
URL = "/v1/assets/connector-icons"


def test_the_community_package_holds_no_connector_icon():
    assert not (COMMUNITY / "assets" / "connector_icons").exists()
    named = sorted(
        str(path.relative_to(REPO))
        for path in COMMUNITY.rglob("*")
        if path.is_file() and path.suffix in (".svg", ".png", ".webp", ".ico") and path.stem in CONNECTOR_TYPES
    )
    assert named == []
    # ... and no Community module still speaks of the route or the directory.
    speaking = sorted(
        str(path.relative_to(REPO))
        for path in COMMUNITY.rglob("*.py")
        if "connector-icons" in path.read_text(encoding="utf-8")
        or "connector_icons" in path.read_text(encoding="utf-8")
    )
    assert speaking == []


def test_a_community_application_has_no_connector_icon_route():
    app = create_community_app()
    assert not [path for path in app.openapi()["paths"] if path.startswith(URL)]
    assert not [route.path for route in app.routes if getattr(route, "path", "").startswith(URL)]
    # Its routes on a bare application: the middleware of the real one asks
    # a database for the tenant before any route is looked for.
    bare = FastAPI()
    bare.router.routes.extend(app.router.routes)
    client = TestClient(bare)
    for connector_type in CONNECTOR_TYPES:
        r = client.get(f"{URL}/{connector_type}.svg")
        assert r.status_code == 404, (connector_type, r.status_code)
        assert b"<svg" not in r.content
    # The other public assets are still the Community application's.
    assert any(path.startswith("/v1/assets/users/") for path in app.openapi()["paths"])
