# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the guardrail provider connection probe
(`app/services/guardrails/provider_probe.py`).

The probe is the one check that distinguishes "reachable + authenticated"
from "silently failing open" — the exact gap that let a `presedio`
hostname typo pass unnoticed under `fail_open: true`. These tests cover:

  * required-field validation (no network call made)
  * 200 → healthy, 401/403 → unhealthy (auth rejected)
  * connection error → unhealthy with a "cannot reach" message
  * timeout → status "timeout"
  * unknown provider → unhealthy (never raises)
  * timeout-window clamping

HTTP is faked (no respx/pytest_httpx in the env) by monkeypatching
`provider_probe.httpx.AsyncClient`. Async probes are driven with
asyncio.run so no pytest-asyncio plugin is required.

Run: pytest tests/test_guardrail_provider_probe.py -v
"""

import asyncio

import httpx
import pytest

from app.services.guardrails import provider_probe
from app.services.guardrails.provider_probe import probe_provider, _timeout_s


def run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient.

    `handler(method, url, **kwargs)` returns a _FakeResp or raises an
    httpx exception to simulate timeout / connection failure.
    """

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return self._handler("GET", url, **kw)

    async def post(self, url, **kw):
        return self._handler("POST", url, **kw)


def _patch_http(monkeypatch, handler):
    monkeypatch.setattr(
        provider_probe.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient(handler),
    )


# ---------------------------------------------------------------------------
# Required-field validation — no network call should be made
# ---------------------------------------------------------------------------

def test_presidio_missing_endpoint_is_unhealthy():
    res = run(probe_provider("presidio", None, None, None))
    assert res.status == "unhealthy"
    assert "endpoint" in res.error.lower()


def test_openai_missing_key_is_unhealthy():
    res = run(probe_provider("openai_moderation", None, None, None))
    assert res.status == "unhealthy"
    assert "key" in res.error.lower()


def test_bedrock_missing_id_is_unhealthy():
    res = run(probe_provider("bedrock", None, None, {"region": "us-east-1"}))
    assert res.status == "unhealthy"
    assert "guardrail_id" in res.error


def test_bedrock_missing_region_is_unhealthy():
    res = run(probe_provider("bedrock", None, None, {"guardrail_id": "gd-1"}))
    assert res.status == "unhealthy"
    assert "region" in res.error


def test_unknown_provider_is_unhealthy_not_raised():
    res = run(probe_provider("acme_secret_scanner", "http://x", None, None))
    assert res.status == "unhealthy"
    assert "no connection probe" in res.error.lower()


# ---------------------------------------------------------------------------
# Presidio — the case that bit us
# ---------------------------------------------------------------------------

def test_presidio_health_200_is_healthy(monkeypatch):
    def handler(method, url, **kw):
        assert url.endswith("/health")
        return _FakeResp(200)
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("presidio", "http://presidio-analyzer:3000", None, None))
    assert res.status == "healthy"
    assert "presidio-analyzer:3000" in res.detail


def test_presidio_hostname_typo_connection_error_is_unhealthy(monkeypatch):
    # The `presedio-analyzer` typo → DNS/connection failure.
    def handler(method, url, **kw):
        raise httpx.ConnectError("Name or service not known")
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("presidio", "http://presedio-analyzer:3000", None, None))
    assert res.status == "unhealthy"
    assert "cannot reach" in res.error.lower()


def test_presidio_health_404_falls_back_to_analyze(monkeypatch):
    def handler(method, url, **kw):
        if url.endswith("/health"):
            return _FakeResp(404)
        assert url.endswith("/analyze")
        return _FakeResp(200, json_data=[])
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("presidio", "http://presidio-analyzer:3000", None, None))
    assert res.status == "healthy"
    assert "/analyze" in res.detail


def test_presidio_timeout_maps_to_timeout(monkeypatch):
    def handler(method, url, **kw):
        raise httpx.TimeoutException("timed out")
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("presidio", "http://slow:3000", None, None))
    assert res.status == "timeout"


# ---------------------------------------------------------------------------
# OpenAI moderation — key validation
# ---------------------------------------------------------------------------

def test_openai_200_is_healthy(monkeypatch):
    def handler(method, url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer sk-test"
        return _FakeResp(200, json_data={"results": [{"flagged": False}]})
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("openai_moderation", None, "sk-test", None))
    assert res.status == "healthy"


def test_openai_401_is_unhealthy(monkeypatch):
    def handler(method, url, **kw):
        return _FakeResp(401, text="invalid api key")
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("openai_moderation", None, "sk-bad", None))
    assert res.status == "unhealthy"
    assert "401" in res.error or "rejected" in res.error.lower()


# ---------------------------------------------------------------------------
# Custom webhook — 2xx healthy, auth header honoured
# ---------------------------------------------------------------------------

def test_custom_webhook_2xx_is_healthy_with_custom_auth_header(monkeypatch):
    seen = {}

    def handler(method, url, **kw):
        seen.update(kw.get("headers", {}))
        return _FakeResp(204)
    _patch_http(monkeypatch, handler)
    res = run(probe_provider(
        "custom",
        "https://gr.example.com/check",
        "secret-token",
        {"auth_header": "X-Api-Key"},
    ))
    assert res.status == "healthy"
    assert seen.get("X-Api-Key") == "secret-token"
    assert "Authorization" not in seen  # custom header used instead of Bearer


def test_custom_webhook_403_flags_auth(monkeypatch):
    def handler(method, url, **kw):
        return _FakeResp(403)
    _patch_http(monkeypatch, handler)
    res = run(probe_provider("custom", "https://gr.example.com/check", "bad", None))
    assert res.status == "unhealthy"
    assert "403" in res.error or "auth" in res.error.lower()


# ---------------------------------------------------------------------------
# Timeout-window clamping
# ---------------------------------------------------------------------------

def test_timeout_honours_config_but_caps_at_ceiling():
    assert _timeout_s({"timeout_ms": 1000}) == 1.0
    # huge operator value is capped to the probe ceiling
    assert _timeout_s({"timeout_ms": 999999}) == provider_probe.DEFAULT_PROBE_TIMEOUT_S
    # absent / invalid → ceiling
    assert _timeout_s(None) == provider_probe.DEFAULT_PROBE_TIMEOUT_S
    assert _timeout_s({"timeout_ms": -5}) == provider_probe.DEFAULT_PROBE_TIMEOUT_S
