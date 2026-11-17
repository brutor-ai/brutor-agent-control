# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Counter deep-link plumbing — backend tests.

Covers the additions to `list_counters` and `CounterScope` that the
deep-link contract relies on:

  * `CounterScope` enum now includes `mcp_server` so a PagerDuty
    alert about an MCP cooldown can construct a valid URL.
  * `list_counters(scope=...)` suppresses row families whose subject
    type doesn't match. The deep link arrives with `scope=mcp_server`
    → LLM rows must NOT appear (and vice versa). Other scopes are
    no-ops at the row generation layer (they're filtered by
    `subject_id` and `group_id` instead).
"""

import typing

from app.api.v1.models.admin.system_ops import CounterScope
from app.services.system_ops.counters import (
    _SCOPE_TO_FAMILIES,
    list_counters,
)


class _FakeDB:
    """SQLAlchemy `Session` stand-in for tests that don't need a real
    DB. The counters service queries `usage_logs`; with no rows in
    the DB the helpers return [] — exactly what we want when the
    test only cares about which row families get invoked, not what
    they emit. Implements the narrow surface SQLAlchemy queries use.
    """

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def group_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class TestCounterScopeEnum:
    def test_includes_mcp_server(self):
        values = typing.get_args(CounterScope)
        assert "mcp_server" in values
        assert "model" in values
        assert "tenant" in values
        # Sanity: the deep-link contract documents these 6 — the
        # admin UI URL constructor will fail closed if the wire enum
        # ever drifts. Stable for one year per the scope doc.
        assert set(values) == {
            "tenant",
            "group",
            "user",
            "api_key",
            "model",
            "mcp_server",
        }


class TestScopeToFamilyMap:
    def test_model_scope_maps_to_llm_family(self):
        assert _SCOPE_TO_FAMILIES["model"] == {"llm"}

    def test_mcp_server_scope_maps_to_mcp_family(self):
        assert _SCOPE_TO_FAMILIES["mcp_server"] == {"mcp"}

    def test_subject_scopes_have_no_family_filter(self):
        # tenant / group / user / api_key are not in the map — they
        # don't suppress row generation, the operator still gets
        # row-level filtering via subject_id / group_id.
        for s in ("tenant", "group", "user", "api_key"):
            assert s not in _SCOPE_TO_FAMILIES


class TestListCountersScopeFilter:
    def test_scope_none_invokes_both_families(self, monkeypatch):
        # Sentinels so we can assert which generator was invoked.
        called = []

        def fake_llm(db, tenant_id, **_kw):
            called.append("llm")
            return [{"kind": "llm_request_rate", "cooldown_active": False}]

        def fake_mcp(db, tenant_id, **_kw):
            called.append("mcp")
            return [{"kind": "mcp_request_rate", "cooldown_active": False}]

        monkeypatch.setattr(
            "app.services.system_ops.counters._llm_counters", fake_llm
        )
        monkeypatch.setattr(
            "app.services.system_ops.counters._mcp_counters", fake_mcp
        )

        rows = list_counters(_FakeDB(), tenant_id="t")
        assert called == ["llm", "mcp"]
        assert len(rows) == 2

    def test_scope_model_suppresses_mcp_family(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.services.system_ops.counters._llm_counters",
            lambda db, tenant_id, **_kw: (
                called.append("llm") or [{"kind": "llm_request_rate", "cooldown_active": False}]
            ),
        )
        monkeypatch.setattr(
            "app.services.system_ops.counters._mcp_counters",
            lambda db, tenant_id, **_kw: (
                called.append("mcp") or [{"kind": "mcp_request_rate", "cooldown_active": False}]
            ),
        )

        rows = list_counters(_FakeDB(), tenant_id="t", scope="model")
        assert called == ["llm"]
        assert len(rows) == 1

    def test_scope_mcp_server_suppresses_llm_family(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.services.system_ops.counters._llm_counters",
            lambda db, tenant_id, **_kw: (
                called.append("llm") or [{"kind": "llm_request_rate", "cooldown_active": False}]
            ),
        )
        monkeypatch.setattr(
            "app.services.system_ops.counters._mcp_counters",
            lambda db, tenant_id, **_kw: (
                called.append("mcp") or [{"kind": "mcp_request_rate", "cooldown_active": False}]
            ),
        )

        rows = list_counters(_FakeDB(), tenant_id="t", scope="mcp_server")
        assert called == ["mcp"]
        assert len(rows) == 1

    def test_scope_tenant_is_passthrough(self, monkeypatch):
        # Subject scopes that don't have a family mapping must
        # behave exactly as scope=None — neither family is
        # suppressed. Otherwise a deep link from a tenant-wide
        # alert would render an empty page when traffic exists.
        called = []
        monkeypatch.setattr(
            "app.services.system_ops.counters._llm_counters",
            lambda db, tenant_id, **_kw: (
                called.append("llm") or [{"kind": "llm_request_rate", "cooldown_active": False}]
            ),
        )
        monkeypatch.setattr(
            "app.services.system_ops.counters._mcp_counters",
            lambda db, tenant_id, **_kw: (
                called.append("mcp") or [{"kind": "mcp_request_rate", "cooldown_active": False}]
            ),
        )

        for s in ("tenant", "group", "user", "api_key"):
            called.clear()
            list_counters(_FakeDB(), tenant_id="t", scope=s)
            assert called == ["llm", "mcp"], f"scope={s} unexpectedly suppressed"
