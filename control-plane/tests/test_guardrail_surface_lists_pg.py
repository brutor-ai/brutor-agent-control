# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a guardrail check's surface list is in PostgreSQL when the
configuration was saved without one: SQL NULL, which the gateway reads as
its default set for the check. A JSON `null` is a value the gateway cannot
use as a list (the check would run nowhere), and a default of the column
is a list nobody chose. Only a database with a JSONB type shows which of
the three was stored.

    BRUTOR_PG_TESTS=1 BRUTOR_PG_TEST_URL=postgresql://USER:PASSWORD@HOST:PORT/postgres pytest tests/test_guardrail_surface_lists_pg.py
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.core.logging_system import BrutorLogger
from app.database.models.guardrail_config import GuardrailConfig
from app.services.guardrails.guardrails_service import GuardrailsService
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

pytestmark = pytest.mark.skipif(os.environ.get("BRUTOR_PG_TESTS") != "1", reason="needs PostgreSQL (BRUTOR_PG_TESTS=1)")

COLUMNS = (
    "pii_surfaces",
    "prompt_injection_surfaces",
    "jailbreak_surfaces",
    "toxic_content_surfaces",
    "banned_words_surfaces",
    "banned_patterns_surfaces",
    "secrets_surfaces",
)
BASE = "/v1/admin/guardrails/configs"


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("guardrail-surfaces-pg")
    for url in throwaway_pg_database("guardrail_surfaces"):
        harness = build_admin_harness(
            [create_guardrails_router(GuardrailsService(logger=log), log, encryption_service=None)], database_url=url
        )
        yield harness
        harness.close()


def _stored(h, config_id) -> dict:
    select = ", ".join(f"{c} IS NULL, {c}::text" for c in COLUMNS)
    with h.Factory() as s:
        row = s.execute(text(f"SELECT {select} FROM guardrail_configs WHERE id = :id"), {"id": config_id}).one()
    return {c: (row[2 * i], row[2 * i + 1]) for i, c in enumerate(COLUMNS)}


def test_a_row_made_without_lists_has_none(h):
    with h.Factory() as s:
        s.add(GuardrailConfig(id="g-omitted", tenant_id="t1", name="omitted"))
        s.add(GuardrailConfig(id="g-none", tenant_id="t1", name="none", **{c: None for c in COLUMNS}))
        s.commit()
    for config_id in ("g-omitted", "g-none"):
        assert _stored(h, config_id) == {c: (True, None) for c in COLUMNS}, config_id


def test_a_configuration_created_through_the_api_without_lists_has_none(h):
    r = h.client.post(BASE, json={"name": "api-default", "builtin": {"secrets_enabled": True}}, headers=h.headers("t1"))
    assert r.status_code in (200, 201), r.text
    body = r.json()
    config_id = body.get("id") or body["config"]["id"]
    assert _stored(h, config_id) == {c: (True, None) for c in COLUMNS}


def test_a_list_that_was_chosen_is_stored_as_it_was_chosen(h):
    chosen = ["chat_input", "mcp_output"]
    r = h.client.post(
        BASE, json={"name": "api-chosen", "builtin": {"secrets_surfaces": chosen, "pii_surfaces": []}}, headers=h.headers("t1")
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    stored = _stored(h, body.get("id") or body["config"]["id"])
    assert stored["secrets_surfaces"] == (False, '["chat_input", "mcp_output"]')
    # An empty list is a choice too: the check runs nowhere.
    assert stored["pii_surfaces"] == (False, "[]")
