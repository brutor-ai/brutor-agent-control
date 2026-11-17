# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM model health checks must be cheap in the background.

Regression (seen on a live dev stack overnight): the Admin UI's AI Models
tab ran a bulk health check every 5 minutes for 8 hours, and every chat /
embedding / audio model got a REAL generation each time (~8,000 provider
calls, ~$3.70; one multi-agent model spent ~10,700 tokens per probe despite
max_tokens=5). Only image / video used the free catalog probe.

Contract pinned here:

  * ``probe="catalog"`` (the UI's timer-driven sweep) asks the core's
    ``probe-catalog`` route — the provider's /models listing, zero tokens —
    for EVERY mode, and never calls a generation endpoint when the provider
    has a catalog URL.
  * ``probe="endpoint"`` (default; an operator's click) still runs the real
    end-to-end probe.
  * A provider without a catalog URL (core answers 422) falls back to the
    endpoint probe, and the result's ``probe_type`` says which probe ran.
  * Background sweeps back off a model whose last ``ai_failure_threshold``
    consecutive checks failed NON-transiently (4xx config / model errors),
    re-checking it at ``ai_interval_seconds * 6`` instead of every interval.
    Transient failures (timeouts, 429, 5xx, network) never back off.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

import app.services.llm.model.llm_models_manager as mgr_mod
from app.services.llm.model.llm_models_manager import LLModelsManager


# ---------------------------------------------------------------------------
# httpx fake — records every URL the manager dispatches to the proxy
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, body: Optional[Dict[str, Any]] = None, text: str = ""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or str(self._body)
        self.headers: Dict[str, str] = {}
        self.content = b""

    def json(self) -> Dict[str, Any]:
        return self._body


_CATALOG_OK = _Resp(
    200,
    {
        "success": True,
        "latency_ms": 12.0,
        "status_code": 200,
        "error_message": "",
        "models_count": 40,
        "model_name_found": True,
        "resolved_url": "https://api.example.test/v1/models",
    },
)
_CHAT_OK = _Resp(
    200,
    {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 7}},
)
_EMBED_OK = _Resp(
    200,
    {"data": [{"embedding": [0.1, 0.2]}], "usage": {"total_tokens": 2}},
)
_NO_CATALOG = _Resp(422, {"error": "no_catalog_url"})


class _Calls(list):
    """URLs posted to, plus the per-suffix response table."""

    routes: Dict[str, _Resp]


@pytest.fixture()
def calls(monkeypatch) -> _Calls:
    """Patch ``httpx.AsyncClient`` in the manager module and record every
    URL the probe posts to. Responses are chosen by route suffix and can be
    overridden per test through ``calls.routes``."""
    out = _Calls()
    out.routes = {
        "/probe-catalog": _CATALOG_OK,
        "/chat/completions": _CHAT_OK,
        "/embeddings": _EMBED_OK,
    }

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url: str, **kw):
            out.append(url)
            for suffix, resp in out.routes.items():
                if url.endswith(suffix):
                    return resp
            raise AssertionError(f"unexpected probe URL {url}")

    monkeypatch.setattr(mgr_mod.httpx, "AsyncClient", _FakeClient)
    return out


def _model(mode: str = "chat", **over) -> MagicMock:
    m = MagicMock()
    m.id = over.pop("id", "llm-cost-test")
    m.tenant_id = "default"
    m.model_name = over.pop("model_name", "gpt-test")
    m.mode = mode
    m.auth_type = "api_key"
    m.api_key = "sk-test"
    m.is_routing = False
    m.is_embedding = mode == "embedding"
    m.is_completion = mode == "completion"
    m.is_responses = mode == "responses"
    m.capabilities = {}
    m.provider = MagicMock(key="openai")
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _manager() -> LLModelsManager:
    mgr = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())
    mgr._reset_proxy_resilience = AsyncMock()
    mgr._record_probe_result = AsyncMock()
    return mgr


def _hit(urls, suffix: str) -> bool:
    return any(u.endswith(suffix) for u in urls)


# ---------------------------------------------------------------------------
# B — probe selection
# ---------------------------------------------------------------------------


class TestBackgroundProbeIsCatalog:
    @pytest.mark.asyncio
    async def test_background_chat_check_uses_catalog_not_generation(self, calls):
        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert _hit(calls, "/probe-catalog")
        assert not _hit(calls, "/chat/completions"), (
            "a background check must not run a real generation when the "
            "provider has a catalog URL"
        )
        assert result["status"] == "healthy"
        assert result["probe_type"] == "catalog"

    @pytest.mark.asyncio
    async def test_background_embedding_check_uses_catalog(self, calls):
        result = await _manager().test_connection(_model("embedding"), MagicMock(), probe="catalog")

        assert _hit(calls, "/probe-catalog")
        assert not _hit(calls, "/embeddings")
        assert result["probe_type"] == "catalog"

    @pytest.mark.asyncio
    async def test_manual_check_still_runs_the_endpoint_probe(self, calls):
        result = await _manager().test_connection(_model("chat"), MagicMock())

        assert _hit(calls, "/chat/completions")
        assert not _hit(calls, "/probe-catalog")
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    async def test_explicit_endpoint_probe_runs_generation(self, calls):
        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="endpoint")

        assert _hit(calls, "/chat/completions")
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    async def test_provider_without_catalog_url_falls_back_to_endpoint(self, calls):
        calls.routes["/probe-catalog"] = _NO_CATALOG

        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert _hit(calls, "/probe-catalog")
        assert _hit(calls, "/chat/completions")
        assert result["status"] == "healthy"
        # Honest badge: the endpoint probe is what actually ran.
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    async def test_background_catalog_success_does_not_reset_breaker(self, calls):
        """A /models listing proves key + reachability, not that generation
        works. A background sweep must not clear an open circuit breaker
        on that evidence — only an operator's endpoint probe may."""
        mgr = _manager()
        await mgr.test_connection(_model("chat"), MagicMock(), probe="catalog")
        mgr._reset_proxy_resilience.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manual_success_still_resets_breaker(self, calls):
        mgr = _manager()
        await mgr.test_connection(_model("chat"), MagicMock())
        mgr._reset_proxy_resilience.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_probe_value_is_rejected(self, calls):
        with pytest.raises(ValueError):
            await _manager().test_connection(_model("chat"), MagicMock(), probe="free")


# ---------------------------------------------------------------------------
# C — back-off classification and decision
# ---------------------------------------------------------------------------


class TestNonTransientClassifier:
    @pytest.mark.parametrize(
        "error",
        [
            "Proxy returned 401: invalid api key",
            "Proxy returned 404: model not found",
            "Proxy returned 400: unsupported parameter",
            "catalog probe returned HTTP 403: forbidden",
            "provider returned 401 — body: unauthorized",
            'model "gpt-x" not found in provider listing of 40 models',
            "API key not configured — set the provider API key on this model",
            "Requires Pro tier",
        ],
    )
    def test_config_and_model_errors_are_non_transient(self, error):
        assert mgr_mod.is_non_transient_probe_failure(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            "Proxy returned 429: rate limited",
            "Proxy returned 408: request timeout",
            "Proxy returned 500: internal",
            "Proxy returned 503: overloaded",
            "catalog probe returned HTTP 502: bad gateway",
            "network/dispatch error: connection refused",
            "ReadTimeout",
            "timeout",
            "",
            None,
            "Unexpected error: boom",
        ],
    )
    def test_transient_or_unknown_errors_never_back_off(self, error):
        assert mgr_mod.is_non_transient_probe_failure(error) is False


def _failing(status="unhealthy", error="Proxy returned 404: no such model", failures=3, ago_s=60.0):
    m = MagicMock()
    m.health_status = status
    m.health_check_error = error
    m.consecutive_health_failures = failures
    m.last_health_check = datetime.now(timezone.utc) - timedelta(seconds=ago_s)
    return m


class TestBackoffDecision:
    def test_backs_off_after_threshold_non_transient_failures(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(), failure_threshold=3, backoff_interval_seconds=1800
        ) is True

    def test_below_threshold_is_checked(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(failures=2), failure_threshold=3, backoff_interval_seconds=1800
        ) is False

    def test_transient_last_failure_is_checked(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(error="Proxy returned 503: overloaded"),
            failure_threshold=3,
            backoff_interval_seconds=1800,
        ) is False

    def test_due_after_the_backoff_interval(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(ago_s=1801), failure_threshold=3, backoff_interval_seconds=1800
        ) is False

    def test_healthy_model_is_never_backed_off(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(status="healthy", error=None, failures=0),
            failure_threshold=3,
            backoff_interval_seconds=1800,
        ) is False

    def test_requires_tier_backs_off(self):
        assert mgr_mod.should_back_off_background_check(
            _failing(status="requires_tier", error="Requires Pro tier"),
            failure_threshold=3,
            backoff_interval_seconds=1800,
        ) is True

    def test_never_checked_is_not_backed_off(self):
        m = _failing()
        m.last_health_check = None
        assert mgr_mod.should_back_off_background_check(
            m, failure_threshold=3, backoff_interval_seconds=1800
        ) is False

    def test_naive_timestamp_from_sqlite_is_handled(self):
        m = _failing()
        m.last_health_check = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)
        assert mgr_mod.should_back_off_background_check(
            m, failure_threshold=3, backoff_interval_seconds=1800
        ) is True


# ---------------------------------------------------------------------------
# HTTP — POST /v1/admin/llms/health-check/bulk threads `probe` and backs off
# ---------------------------------------------------------------------------


@pytest.fixture()
def bulk():
    """Real app + SQLite harness; ``test_connection`` is replaced by a
    recorder so the test sees exactly which models were probed and with
    which ``probe`` value — no network."""
    from tests.admin_endpoint_harness import build_admin_harness
    from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
    from app.core.logging_system import BrutorLogger
    from app.database.models.llm_provider import LlmProvider

    log = BrutorLogger("test-llm-health-cost")
    manager = LLModelsManager(logger=log, encryption_service=None)
    probed: List[Dict[str, Any]] = []

    async def _fake_test_connection(model, db, timeout=None, probe="endpoint"):
        probed.append({"id": model.id, "probe": probe})
        return {
            "model_id": model.id,
            "model_name": model.model_name,
            "status": "healthy",
            "response_time_ms": 1.0,
            "error_message": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "probe_type": "catalog" if probe == "catalog" else "endpoint",
        }

    manager.test_connection = _fake_test_connection  # type: ignore[assignment]
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=None)],
        logger_name="test-llm-health-cost",
    )
    harness.seed(
        lambda s: s.add(
            LlmProvider(id="prov-t1", tenant_id="t1", key="openai", display_name="OpenAI")
        )
    )
    harness.probed = probed  # type: ignore[attr-defined]
    yield harness
    harness.close()


def _seed_model(h, mid: str, **health):
    from app.database.models.llm_model import LLModel

    def fn(s):
        m = LLModel(
            id=mid,
            tenant_id="t1",
            name=mid,
            model_name=mid,
            provider_id="prov-t1",
            mode="chat",
            enabled=True,
        )
        for k, v in health.items():
            setattr(m, k, v)
        s.add(m)

    h.seed(fn)


def _broken(ago_s: float = 60.0, error: str = "Proxy returned 404: model not found", failures: int = 3):
    return dict(
        health_status="unhealthy",
        health_check_error=error,
        consecutive_health_failures=failures,
        last_health_check=datetime.now(timezone.utc) - timedelta(seconds=ago_s),
    )


def _bulk(h, **body):
    r = h.client.post("/v1/admin/llms/health-check/bulk", json=body, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    return r.json()


class TestBulkEndpoint:
    def test_default_is_the_endpoint_probe(self, bulk):
        _seed_model(bulk, "m-ok")
        _bulk(bulk)
        assert bulk.probed == [{"id": "m-ok", "probe": "endpoint"}]

    def test_background_sweep_requests_the_catalog_probe(self, bulk):
        _seed_model(bulk, "m-ok")
        body = _bulk(bulk, probe="catalog")
        assert bulk.probed == [{"id": "m-ok", "probe": "catalog"}]
        assert body["results"][0]["probe_type"] == "catalog"
        assert body["results"][0]["backed_off"] is False

    def test_unknown_probe_is_rejected(self, bulk):
        _seed_model(bulk, "m-ok")
        r = bulk.client.post(
            "/v1/admin/llms/health-check/bulk", json={"probe": "free"}, headers=bulk.headers("t1")
        )
        assert r.status_code == 422

    def test_background_sweep_backs_off_a_persistently_misconfigured_model(self, bulk):
        _seed_model(bulk, "m-ok")
        _seed_model(bulk, "m-broken", **_broken())
        body = _bulk(bulk, probe="catalog")

        assert [p["id"] for p in bulk.probed] == ["m-ok"]
        row = next(r for r in body["results"] if r["model_id"] == "m-broken")
        assert row["backed_off"] is True
        # The last known verdict is reported, not a fresh "healthy".
        assert row["status"] == "unhealthy"
        assert "404" in (row["error_message"] or "")

    def test_backed_off_model_is_rechecked_after_six_intervals(self, bulk):
        # Default ai_interval_seconds = 300 → back-off window 1800 s.
        _seed_model(bulk, "m-broken", **_broken(ago_s=1801))
        _bulk(bulk, probe="catalog")
        assert [p["id"] for p in bulk.probed] == ["m-broken"]

    def test_transient_failures_never_back_off(self, bulk):
        _seed_model(bulk, "m-flaky", **_broken(error="Proxy returned 503: overloaded"))
        _bulk(bulk, probe="catalog")
        assert [p["id"] for p in bulk.probed] == ["m-flaky"]

    def test_operator_check_never_backs_off(self, bulk):
        _seed_model(bulk, "m-broken", **_broken())
        _bulk(bulk)
        assert bulk.probed == [{"id": "m-broken", "probe": "endpoint"}]

    def test_tenant_failure_threshold_is_honoured(self, bulk):
        from app.database.models.gateway_config import HealthCheckConfig

        def fn(s):
            cfg = HealthCheckConfig(tenant_id="t1", **HealthCheckConfig.get_default_config())
            cfg.ai_failure_threshold = 5
            cfg.ai_interval_seconds = 60
            s.add(cfg)

        bulk.seed(fn)
        # 3 failures < threshold 5 → still probed.
        _seed_model(bulk, "m-broken", **_broken(ago_s=60))
        _bulk(bulk, probe="catalog")
        assert [p["id"] for p in bulk.probed] == ["m-broken"]

    def test_tenant_interval_sets_the_backoff_window(self, bulk):
        from app.database.models.gateway_config import HealthCheckConfig

        def fn(s):
            cfg = HealthCheckConfig(tenant_id="t1", **HealthCheckConfig.get_default_config())
            cfg.ai_interval_seconds = 60  # window = 360 s
            s.add(cfg)

        bulk.seed(fn)
        _seed_model(bulk, "m-recent", **_broken(ago_s=300))
        _seed_model(bulk, "m-due", **_broken(ago_s=400))
        _bulk(bulk, probe="catalog")
        assert [p["id"] for p in bulk.probed] == ["m-due"]


# ---------------------------------------------------------------------------
# Background catalog probe: listing evidence that is about the LISTING, not
# the model, must not paint the model red — fall back to the endpoint probe.
# Shapes mirror the core's CatalogProbeResult (catalog_probe.rs): a 200
# listing without the name reports success=false, status_code=200,
# model_name_found=false; a refused listing reports success=false,
# status_code=401/403, model_name_found=null.
# Live evidence: DeepSeek's /models omits deepseek-v4-flash
# (generation works); Groq's /models answers 403 for a key that generates.
# ---------------------------------------------------------------------------

_CATALOG_NAME_MISSING = _Resp(
    200,
    {
        "success": False,
        "latency_ms": 20.0,
        "status_code": 200,
        "error_message": 'model "deepseek-v4-flash" not found in provider listing of 2 models',
        "models_count": 2,
        "model_name_found": False,
        "resolved_url": "https://api.deepseek.test/models",
        "provider_key": "deepseek",
    },
)


def _catalog_listing_refused(status: int) -> _Resp:
    return _Resp(
        200,
        {
            "success": False,
            "latency_ms": 20.0,
            "status_code": status,
            "error_message": f"provider returned {status} — body: forbidden",
            "models_count": None,
            "model_name_found": None,
            "resolved_url": "https://api.groq.test/openai/v1/models",
            "provider_key": "groq",
        },
    )


class TestBackgroundCatalogFallsBackOnListingOnlyEvidence:
    @pytest.mark.asyncio
    async def test_name_missing_from_200_listing_falls_back_to_endpoint(self, calls):
        calls.routes["/probe-catalog"] = _CATALOG_NAME_MISSING

        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert _hit(calls, "/probe-catalog")
        assert _hit(calls, "/chat/completions")
        assert result["status"] == "healthy"
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_refused_listing_falls_back_to_endpoint(self, calls, status):
        calls.routes["/probe-catalog"] = _catalog_listing_refused(status)

        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert _hit(calls, "/chat/completions")
        assert result["status"] == "healthy"
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    async def test_name_missing_embedding_falls_back_to_embedding_probe(self, calls):
        calls.routes["/probe-catalog"] = _CATALOG_NAME_MISSING

        result = await _manager().test_connection(_model("embedding"), MagicMock(), probe="catalog")

        assert _hit(calls, "/embeddings")
        assert result["probe_type"] == "endpoint"

    @pytest.mark.asyncio
    async def test_fallback_endpoint_failure_is_still_unhealthy_and_backs_off(self, calls):
        """The fallback is a real verdict: a model the listing omits AND
        whose endpoint 404s is unhealthy, with an error the back-off
        classifier treats as non-transient."""
        calls.routes["/probe-catalog"] = _CATALOG_NAME_MISSING
        calls.routes["/chat/completions"] = _Resp(404, {}, text="model does not exist")

        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert result["status"] == "unhealthy"
        assert result["probe_type"] == "endpoint"
        assert mgr_mod.is_non_transient_probe_failure(result["error_message"]) is True

    @pytest.mark.asyncio
    async def test_provider_5xx_on_listing_is_still_a_catalog_failure(self, calls):
        """Only listing-auth refusals and name-absence fall back; a provider
        outage on the listing is reported as the catalog verdict (transient,
        so it never backs off) without spending tokens."""
        calls.routes["/probe-catalog"] = _catalog_listing_refused(503)

        result = await _manager().test_connection(_model("chat"), MagicMock(), probe="catalog")

        assert not _hit(calls, "/chat/completions")
        assert result["status"] == "unhealthy"
        assert result["probe_type"] == "catalog"

    @pytest.mark.asyncio
    async def test_operator_image_check_keeps_catalog_verdict(self, calls):
        """Operator-initiated image/video checks keep today's behaviour:
        the catalog verdict stands (no fallback change on the manual path)."""
        calls.routes["/probe-catalog"] = _CATALOG_NAME_MISSING

        result = await _manager().test_connection(_model("image_generation"), MagicMock())

        assert result["status"] == "unhealthy"
        assert result["probe_type"] == "catalog"
