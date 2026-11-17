# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The product is Brutor Agent Control.

"Brutor AI Gateway" was still what the control plane's API document, its
`/`, `/version` and `/health` answers, a route tag and the FOCUS cost export
said. What a client reads says the decided name; no source of either
package says the old one.
"""

from __future__ import annotations

import json

import pytest

from tests.app_factory import create_community_app
from tests.source_trees import REPO

SERVICE = "Brutor Agent Control Plane"
OLD = "Brutor AI Gateway"


@pytest.fixture(scope="module")
def document():
    return create_community_app().openapi()


def test_the_api_document_is_titled_with_the_product(document):
    assert document["info"]["title"] == SERVICE
    assert "Brutor Agent Control" in document["info"]["description"]


def test_the_api_document_does_not_say_ai_gateway(document):
    text = json.dumps(document)
    assert OLD not in text
    assert "AI Gateway" not in text


def test_the_service_names_itself(document):
    from app.api.v1.endpoints import service_info

    assert service_info.SERVICE_NAME == SERVICE
    source = (REPO / "app/api/v1/endpoints/service_info.py").read_text(encoding="utf-8")
    # `/`, `/version` and `/health` all answer the one constant.
    assert source.count('"service": SERVICE_NAME') == 3
    assert '"service": "' not in source


def test_no_source_says_the_old_name():
    found = sorted(
        path.relative_to(REPO).as_posix()
        for root in ("app", "enterprise/brutor_enterprise")
        if (REPO / root).exists()
        for path in (REPO / root).rglob("*.py")
        if OLD in path.read_text(encoding="utf-8")
    )
    assert found == []


# --- the control plane's own name and the image label -----------------------

OLD_SERVICE = "Brutor Gateway Control Plane"
COMPANY = "Brutor AI Ltd"
# What a start logs and what an operator opens, beside the two packages.
OPERATOR_FILES = ("gunicorn.conf.py", "alembic.ini", "main.py", "start_with_gunicorn.sh", "Dockerfile")


def test_nothing_the_control_plane_logs_or_ships_says_gateway_control_plane():
    found = sorted(
        path.relative_to(REPO).as_posix()
        for root in ("app", "enterprise/brutor_enterprise")
        if (REPO / root).exists()
        for path in (REPO / root).rglob("*.py")
        if OLD_SERVICE in path.read_text(encoding="utf-8")
    )
    found += [name for name in OPERATOR_FILES if OLD_SERVICE in (REPO / name).read_text(encoding="utf-8")]
    assert found == []
    # The server's start, ready and stop lines name the service.
    gunicorn = (REPO / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert gunicorn.count(SERVICE) >= 4


def test_the_default_tenant_is_branded_with_the_product():
    from app.database.models.tenant import Tenant

    source = (REPO / "app/database/__init__.py").read_text(encoding="utf-8")
    assert 'default_tenant.theme_company_name = "Brutor Agent Control"' in source
    assert Tenant.create_default_tenant is not None


def test_the_image_is_labelled_with_the_company_not_a_person():
    """The vendor label names the company; no label names a maintainer (the
    labels are `scripts/image-names.txt`'s, `tests/test_image_names.py`)."""
    lines = [line.strip() for line in (REPO / "Dockerfile").read_text(encoding="utf-8").splitlines()]
    assert f'LABEL org.opencontainers.image.vendor="{COMPANY}"' in lines
    assert not [line for line in lines if line.lower().startswith("label maintainer")]
