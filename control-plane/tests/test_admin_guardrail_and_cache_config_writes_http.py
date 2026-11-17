# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Full-body writes to guardrail configs and semantic cache configs: every section of
the document lands in its column, and neither reaches another tenant.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.guardrail_config import GuardrailConfig
from app.database.models.semantic_cache_config import SemanticCacheConfig
from app.services.guardrails.guardrails_service import GuardrailsService
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

GR = "/v1/admin/guardrails"
SC = "/v1/admin/semantic-cache"
_n = {"i": 0}


def _next() -> str:
    _n["i"] += 1
    return f"{_n['i']:04d}"


FULL = {
    "description": "full",
    "priority": 7,
    "enabled": True,
    "streaming_output_mode": "sync",
    "scope_tool_name": " read_file ",
    "surfaces": {"chat_input": False, "chat_output": True, "mcp_input": False, "mcp_output": False,
                 "skill_input": False, "a2a_inbound": True, "a2a_outbound": True},
    "builtin": {
        "pii_enabled": True, "pii_action": "block", "pii_threshold": 0.4, "pii_surfaces": ["chat_input"],
        "prompt_injection_enabled": False, "prompt_injection_action": "warn", "prompt_injection_sensitivity": 0.2,
        "prompt_injection_surfaces": ["chat_input"], "jailbreak_enabled": False, "jailbreak_action": "warn",
        "jailbreak_surfaces": ["chat_input"], "toxic_content_enabled": False, "toxic_content_action": "block",
        "toxic_content_threshold": 0.3, "toxic_content_surfaces": ["chat_input"], "banned_words": ["alpha"],
        "banned_patterns": ["beta.*"], "banned_words_surfaces": ["chat_input"], "banned_patterns_surfaces": ["chat_input"],
        "secrets_enabled": True, "secrets_action": "block", "secrets_categories": ["aws_access_key"], "secrets_surfaces": ["chat_input"],
        "secrets_threshold": 0.6,
    },
    "max_check_timeout_ms": 1500.0,
    "fail_open": True,
    "log_all_checks": True,
}


def harness_with(extra_routers=()):
    """The file's harness: the guardrails router, and the routers an edition
    adds (the Enterprise half mounts the semantic-cache router)."""
    log = BrutorLogger("test-config-writes")
    for url in throwaway_pg_database("config_writes"):
        harness = build_admin_harness(
            [create_guardrails_router(GuardrailsService(logger=log), log), *extra_routers],
            database_url=url,
        )
        yield harness
        harness.close()


@pytest.fixture(scope="module")
def h():
    yield from harness_with()


def _gr(h, tenant="t1", **over) -> str:
    r = h.client.post(f"{GR}/configs", json={"name": f"gr-{_next()}", "enabled": True, **over}, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    payload = r.json()
    return payload.get("config", payload)["id"]


def _gr_row(h, cid):
    s = h.Factory()
    try:
        row = s.get(GuardrailConfig, cid)
        if row is not None:
            _ = [g.id for g in row.groups]
            s.expunge(row)
        return row
    finally:
        s.close()


class TestGuardrailPatch:
    def test_every_section_lands_in_its_column(self, h):
        cid = _gr(h)
        r = h.client.patch(f"{GR}/configs/{cid}", json=FULL, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _gr_row(h, cid)
        assert (row.description, row.priority, row.streaming_output_mode) == ("full", 7, "sync")
        assert row.scope_tool_name == "read_file"
        assert (row.chat_input_enabled, row.chat_output_enabled, row.a2a_inbound_enabled) == (False, True, True)
        assert (row.pii_action, row.pii_threshold, row.pii_surfaces) == ("block", 0.4, ["chat_input"])
        assert (row.prompt_injection_enabled, row.prompt_injection_sensitivity) == (False, 0.2)
        assert (row.toxic_content_action, row.toxic_content_threshold) == ("block", 0.3)
        assert (row.banned_words, row.banned_patterns) == (["alpha"], ["beta.*"])
        assert (row.secrets_detection_enabled, row.secrets_action, row.secrets_categories) == (True, "block", ["aws_access_key"])
        assert (row.max_check_timeout_ms, row.fail_open, row.log_all_checks) == (1500.0, True, True)

    def test_the_same_document_through_the_tenant_route(self, h):
        s = h.Factory()
        try:
            s.query(GuardrailConfig).filter_by(tenant_id="t2").delete()
            s.commit()
        finally:
            s.close()
        r = h.client.patch(f"{GR}/config", json={**FULL, "name": "tenant-one"}, headers=h.headers("t2"))
        assert r.status_code == 200, r.text
        s = h.Factory()
        try:
            row = s.query(GuardrailConfig).filter_by(tenant_id="t2").one()
            assert (row.name, row.pii_action, row.fail_open) == ("tenant-one", "block", True)
        finally:
            s.close()

    def test_a_foreign_group_is_not_bound(self, h):
        cid = _gr(h)
        r = h.client.patch(f"{GR}/configs/{cid}", json={"enabled": True, "group_ids": ["group-default-t2"]}, headers=h.headers("t1"))
        assert r.status_code in (200, 400, 404), r.text
        assert "group-default-t2" not in [g.id for g in _gr_row(h, cid).groups]

    def test_own_group_is_bound_and_replaced(self, h):
        cid = _gr(h)
        r = h.client.patch(f"{GR}/configs/{cid}", json={"enabled": True, "group_ids": ["group-default-t1"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert [g.id for g in _gr_row(h, cid).groups] == ["group-default-t1"]
        r = h.client.patch(f"{GR}/configs/{cid}", json={"enabled": True, "group_ids": []}, headers=h.headers("t1"))
        assert [g.id for g in _gr_row(h, cid).groups] == []

    def test_setting_a_default_clears_the_previous_one(self, h):
        a, b = _gr(h), _gr(h)
        h.client.patch(f"{GR}/configs/{a}", json={"enabled": True, "is_default": True}, headers=h.headers("t1"))
        h.client.patch(f"{GR}/configs/{b}", json={"enabled": True, "is_default": True}, headers=h.headers("t1"))
        assert _gr_row(h, a).is_default is False and _gr_row(h, b).is_default is True

    def test_another_tenants_config_looks_absent(self, h):
        cid = _gr(h, "t2")
        before = _gr_row(h, cid).description
        for r in (
            h.client.get(f"{GR}/configs/{cid}", headers=h.headers("t1")),
            h.client.patch(f"{GR}/configs/{cid}", json={"enabled": True, "description": "stolen"}, headers=h.headers("t1")),
            h.client.delete(f"{GR}/configs/{cid}", headers=h.headers("t1")),
        ):
            assert r.status_code == 404, r.text
        assert _gr_row(h, cid).description == before

    def test_read_permission_cannot_write(self, h):
        cid = _gr(h)
        ro = h.limited_headers("t1", [Permission.GUARDRAIL_READ])
        assert h.client.patch(f"{GR}/configs/{cid}", json={"enabled": False}, headers=ro).status_code == 403
        assert _gr_row(h, cid).enabled is True


def _sc(h, tenant="t1", **over) -> str:
    r = h.client.post(f"{SC}/configs", json={"name": f"sc-{_next()}", **over}, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _sc_row(h, cid):
    s = h.Factory()
    try:
        row = s.get(SemanticCacheConfig, cid)
        return None if row is None else {"name": row.name, "enabled": row.enabled, "tenant": row.tenant_id}
    finally:
        s.close()


