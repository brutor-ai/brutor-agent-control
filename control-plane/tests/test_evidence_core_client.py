# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Core evidence client

Every function in `app/services/evidence/core_client.py` must never raise
and must map core failures onto the contract's error codes, with bounded
detail. `httpx.request` is replaced by a recorder, so these tests pin the
exact method, path, body and timeout each function sends.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from app.services.evidence import core_client

BASE = "http://core.test:8100"


class _Recorder:
    def __init__(self, status: int = 200, body: Any = None, text: str | None = None, exc: Exception | None = None):
        self.status, self.body, self.text, self.exc = status, body, text, exc
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, method, url, *, json=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.exc is not None:
            raise self.exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.body if self.body is not None else {})

    @property
    def last(self) -> Dict[str, Any]:
        return self.calls[-1]


@pytest.fixture()
def core(monkeypatch):
    monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", BASE)
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "hc-key")

    def install(**kw) -> _Recorder:
        rec = _Recorder(**kw)
        monkeypatch.setattr(core_client.httpx, "request", rec)
        return rec

    return install


# ── request shape per function ─────────────────────────────────────────


class TestRequestShape:
    def test_seal(self, core):
        rec = core(body={"sealed": [{"record_id": "r1", "log_id": "l", "seq": 0}]})
        out = core_client.seal("default", [{"record_type": "ai.brutor.declaration"}])
        assert out == {"sealed": [{"record_id": "r1", "log_id": "l", "seq": 0}]}
        call = rec.last
        assert (call["method"], call["url"]) == ("POST", f"{BASE}/v1/evidence/seal")
        assert call["json"] == {
            "tenant_id": "default",
            "records": [{"record_type": "ai.brutor.declaration"}],
        }
        assert call["timeout"] == core_client.SEAL_TIMEOUT_SECONDS
        assert call["headers"]["Authorization"] == "Bearer hc-key"

    def test_force_tree_head_encodes_tenant(self, core):
        rec = core(body={"issued": False, "head": None})
        assert core_client.force_tree_head("a/b c") == {"issued": False, "head": None}
        assert rec.last["url"] == f"{BASE}/v1/evidence/logs/a%2Fb%20c/tree-head"

    def test_verify_statement_omits_absent_optionals(self, core):
        rec = core(body={"verified": True})
        core_client.verify_statement("AAE=")
        assert rec.last["url"].endswith("/v1/evidence/verify/statement")
        assert rec.last["json"] == {"statement_b64": "AAE="}
        core_client.verify_statement("AAE=", {"k": 1}, trusted_kids=["kid1"])
        assert rec.last["json"] == {"statement_b64": "AAE=", "record": {"k": 1}, "trusted_kids": ["kid1"]}

    def test_verify_tree_head(self, core):
        rec = core(body={"verified": True, "tree_size": 3})
        out = core_client.verify_tree_head("S2", previous_statement_b64="S1")
        assert out["tree_size"] == 3
        assert rec.last["url"].endswith("/v1/evidence/verify/tree-head")
        assert rec.last["json"] == {"statement_b64": "S2", "previous_statement_b64": "S1"}

    def test_verify_receipt(self, core):
        rec = core(body={"grade": "witnessed", "errors": []})
        out = core_client.verify_receipt("R", "S", witness_id="wit-1")
        assert out["grade"] == "witnessed"
        assert rec.last["url"].endswith("/v1/evidence/verify/receipt")
        assert rec.last["json"] == {"receipt_b64": "R", "statement_b64": "S", "witness_id": "wit-1"}

    def test_verify_receipt_rejects_both_key_sources_without_calling(self, core):
        rec = core()
        out = core_client.verify_receipt("R", "S", pinned_key_hex="ab", witness_id="w")
        assert out["error"] == core_client.ERROR_INVALID_RECORD
        assert rec.calls == []

    def test_verify_bundle_uses_longer_timeout(self, core):
        rec = core(body={"ok": True, "findings": []})
        core_client.verify_bundle({"manifest": {}})
        assert rec.last["url"].endswith("/v1/evidence/verify/bundle")
        assert rec.last["timeout"] == core_client.VERIFY_BUNDLE_TIMEOUT_SECONDS

    def test_build_bundle(self, core):
        rec = core(body={"bundle_id": "bdl-1", "bundle_digest": "d" * 64, "record_id": "r", "bundle": {}})
        out = core_client.build_bundle(
            "default", {"root_task_id": "run-1"}, audience="Auditor X", requested_by="u-1"
        )
        assert out["bundle_id"] == "bdl-1"
        call = rec.last
        assert call["url"] == f"{BASE}/v1/evidence/bundles"
        assert call["timeout"] == core_client.BUNDLE_TIMEOUT_SECONDS
        assert call["json"] == {
            "tenant_id": "default",
            "selector": {"root_task_id": "run-1"},
            "audience": "Auditor X",
            "payloads": "none",
            "closure_depth": 1,
            "requested_by": "u-1",
        }

    def test_build_bundle_requires_selector(self, core):
        rec = core()
        assert core_client.build_bundle("default", {})["error"] == core_client.ERROR_INVALID_RECORD
        assert rec.calls == []

    def test_backfill_clamps_limit(self, core):
        rec = core(body={"sealed": 0})
        core_client.backfill("default", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z", limit=50_000)
        assert rec.last["url"].endswith("/v1/evidence/backfill")
        assert rec.last["json"]["limit"] == core_client.MAX_BACKFILL_LIMIT
        assert rec.last["json"]["from"] == "2026-09-01T00:00:00Z"
        assert rec.last["timeout"] == core_client.BACKFILL_TIMEOUT_SECONDS

    def test_rotate_key(self, core):
        rec = core(body={"kid": "k" * 64, "rotation_record_id": "r"})
        assert core_client.rotate_key("default")["kid"] == "k" * 64
        assert rec.last["url"] == f"{BASE}/v1/evidence/keys/default/rotate"


# ── seal guards ────────────────────────────────────────────────────────


class TestSealGuards:
    def test_empty_is_a_noop(self, core):
        rec = core()
        assert core_client.seal("default", []) == {"sealed": []}
        assert rec.calls == []

    def test_over_100_is_rejected_locally(self, core):
        rec = core()
        out = core_client.seal("default", [{"record_type": "x"}] * 101)
        assert out["error"] == core_client.ERROR_INVALID_RECORD
        assert rec.calls == []

    def test_missing_record_type_is_rejected_locally(self, core):
        rec = core()
        assert core_client.seal("default", [{"verdict": "recorded"}])["error"] == "invalid_record"
        assert rec.calls == []

    def test_response_without_sealed_list_is_a_core_error(self, core):
        core(body={"ok": True})
        assert core_client.seal("default", [{"record_type": "x"}])["error"] == core_client.ERROR_CORE


# ── error mapping (never raises) ───────────────────────────────────────


ALL_CALLS = [
    lambda: core_client.seal("default", [{"record_type": "x"}]),
    lambda: core_client.force_tree_head("default"),
    lambda: core_client.verify_statement("S"),
    lambda: core_client.verify_tree_head("S"),
    lambda: core_client.verify_receipt("R", "S"),
    lambda: core_client.verify_bundle({}),
    lambda: core_client.build_bundle("default", {"record_ids": ["r"]}),
    lambda: core_client.backfill("default", "a", "b"),
    lambda: core_client.rotate_key("default"),
]


class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,code",
        [
            (503, "evidence_disabled"),
            (422, "invalid_record"),
            (409, "payloads_unavailable"),
            (500, "core_error"),
            (404, "core_error"),
            (401, "core_error"),
        ],
    )
    @pytest.mark.parametrize("call", ALL_CALLS)
    def test_status_codes(self, core, call, status, code):
        core(status=status, body={"error": code, "detail": "why"})
        out = call()
        assert out == {"error": code, "detail": f"HTTP {status}: why"}

    @pytest.mark.parametrize("call", ALL_CALLS)
    def test_a_503_that_names_the_tenants_key_is_not_evidence_disabled(self, core, call):
        """The gateway answers 503 for two different things: no signing key
        is configured (`evidence_disabled`), and this tenant's own key
        cannot be used (`tenant_key_unavailable`). The second is passed on
        as itself, with the gateway's words."""
        detail = "this tenant's own evidence signing key cannot be used; nothing is sealed until it is rotated"
        core(status=503, body={"error": "tenant_key_unavailable", "detail": detail})
        out = call()
        assert out == {"error": "tenant_key_unavailable", "detail": f"HTTP 503: {detail}"}
        assert out["error"] == core_client.ERROR_TENANT_KEY_UNAVAILABLE != core_client.ERROR_EVIDENCE_DISABLED

    @pytest.mark.parametrize("body", [{"error": "evidence_disabled", "detail": "why"}, {"detail": "why"}, None])
    def test_any_other_503_is_evidence_disabled(self, core, body):
        core(status=503, body=body) if body is not None else core(status=503, text="upstream unavailable")
        assert core_client.rotate_key("default")["error"] == core_client.ERROR_EVIDENCE_DISABLED

    def test_the_code_is_the_gateways(self):
        from tests.core_repo import core_revision, core_source

        routes = core_source("routes", "evidence.rs").read_text(encoding="utf-8")
        read = f" [the gateway's source read: {core_revision()}]"
        assert f'"{core_client.ERROR_TENANT_KEY_UNAVAILABLE}",' in routes, read
        assert f'"{core_client.ERROR_EVIDENCE_DISABLED}",' in routes, read
        assert "StatusCode::SERVICE_UNAVAILABLE" in routes, read

    @pytest.mark.parametrize("call", ALL_CALLS)
    def test_transport_failure_is_core_unreachable(self, core, call):
        core(exc=httpx.ConnectError("connection refused"))
        out = call()
        assert out["error"] == "core_unreachable"
        assert "connection refused" in out["detail"]

    def test_timeout_is_core_unreachable(self, core):
        core(exc=httpx.ReadTimeout("timed out"))
        assert core_client.rotate_key("default")["error"] == "core_unreachable"

    def test_error_detail_is_bounded(self, core):
        core(status=502, text="<html>" + "x" * 10_000 + "\n</html>")
        out = core_client.force_tree_head("default")
        assert out["error"] == "core_error"
        assert len(out["detail"]) <= core_client.MAX_DETAIL_CHARS
        assert "\n" not in out["detail"]

    def test_non_json_success_is_core_error(self, core):
        core(status=200, text="not json")
        assert core_client.verify_statement("S")["error"] == "core_error"

    def test_non_object_success_is_core_error(self, core):
        core(status=200, body=[1, 2])
        assert core_client.verify_statement("S")["error"] == "core_error"

    def test_is_error(self):
        assert core_client.is_error({"error": "core_error", "detail": ""})
        assert not core_client.is_error({"sealed": []})

    def test_really_unreachable_core(self, monkeypatch):
        """No monkeypatched transport: a closed port must come back as an
        error dict, not an exception."""
        monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://127.0.0.1:9")
        out = core_client.force_tree_head("default", timeout=1.0)
        assert out["error"] == "core_unreachable"


class TestVerifyRecordKeepsItsShape:
    def test_unreachable_is_a_finding(self, core):
        core(exc=httpx.ConnectError("refused"))
        out = core_client.verify_record({}, None)
        assert out["ok"] is False and out["record_id"] is None
        assert out["findings"][0]["code"] == core_client.VERIFIER_UNAVAILABLE
        assert out["findings"][0]["detail"].startswith("verifier unreachable")

    def test_http_error_is_a_finding(self, core):
        core(status=500, text="boom")
        out = core_client.verify_record({}, None)
        assert out["findings"][0]["code"] == core_client.VERIFIER_UNAVAILABLE
        assert "HTTP 500" in out["findings"][0]["detail"]

    def test_success_defaults(self, core):
        rec = core(body={"ok": True})
        out = core_client.verify_record({"a": 1}, "AAE=")
        assert out == {"ok": True, "findings": [], "record_id": None}
        assert rec.last["json"] == {"record": {"a": 1}, "statement_b64": "AAE="}


def test_control_plane_never_imports_cryptography():
    import ast
    from pathlib import Path

    src = Path(core_client.__file__).read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("cryptography") for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("cryptography")
