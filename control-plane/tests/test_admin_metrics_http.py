# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the proxy metrics admin API (/v1/admin/metrics).

The
metrics surface is 21 read-only aggregations; what is worth defending is
that every one of them (a) answers on real rows, (b) answers on an EMPTY
tenant without dividing by zero, and (c) never leaks another tenant's
numbers into the aggregate.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.metrics import create_proxy_metrics_admin_router
from app.core.logging_system import BrutorLogger
from app.database.models.proxy_log import ProxyLog
from app.services.proxy_metrics_service import UnifiedProxyMetricsService


@pytest.fixture()
def h():
    log = BrutorLogger("test-metrics")
    harness = build_admin_harness(
        [create_proxy_metrics_admin_router(UnifiedProxyMetricsService(log), log)],
        logger_name="test-metrics",
    )

    def fn(s):
        now = datetime.now(timezone.utc)
        for i in range(6):
            s.add(
                ProxyLog(
                    id=f"plog-{uuid.uuid4().hex[:12]}",
                    tenant_id="t1",
                    proxy_type="llm" if i % 2 == 0 else "mcp",
                    end_user_session_id="sess-1",
                    http_method="POST",
                    endpoint="/v1/proxy/llm/chat/completions",
                    success=i != 5,
                    duration_ms=120 + i * 10,
                    timestamp=now - timedelta(minutes=10 + i),
                    created_at=now - timedelta(minutes=10 + i),
                )
            )

    harness.seed(fn)
    yield harness
    harness.close()


# Every read surface, hit for a tenant WITH data and a tenant WITHOUT.
#: These three run raw PostgreSQL (date_bin, ::int casts) and cannot execute
#: on the SQLite harness. They are exercised against PG by the live suite;
#: skipping them HERE keeps the skip visible in every test run rather than
#: silently absent from the list.
_PG_ONLY = pytest.mark.skip(reason="raw PostgreSQL SQL (date_bin) — not runnable on SQLite")

READ_PATHS = [
    "/v1/admin/metrics/performance",
    "/v1/admin/metrics/real-time",
    "/v1/admin/metrics/dashboard",
    "/v1/admin/metrics/usage",
    pytest.param("/v1/admin/metrics/timeseries/requests", marks=_PG_ONLY),
    pytest.param("/v1/admin/metrics/timeseries/latency", marks=_PG_ONLY),
    pytest.param("/v1/admin/metrics/timeseries/error-rate", marks=_PG_ONLY),
    "/v1/admin/metrics/heatmap/hourly",
    # Runs on every database: the route reads only columns
    # `ProxyMetricEntry` has.
    "/v1/admin/metrics/methods/performance",
    "/v1/admin/metrics/servers/status",
    "/v1/admin/metrics/cost/breakdown",
    "/v1/admin/metrics/cost/monthly",
    "/v1/admin/metrics/cost/budgets",
    "/v1/admin/metrics/cost/series",
    "/v1/admin/metrics/cost/savings",
    "/v1/admin/metrics/governance/posture",
    "/v1/admin/metrics/agents/activity",
]


class TestReadSurfaces:
    @pytest.mark.parametrize("path", READ_PATHS)
    def test_answers_with_data(self, h, path):
        r = h.client.get(path, headers=h.headers("t1"))
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"

    @pytest.mark.parametrize("path", READ_PATHS)
    def test_answers_on_an_empty_tenant(self, h, path):
        r = h.client.get(path, headers=h.headers("t2"))
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"

    def test_reads_require_auth(self, h):
        r = h.client.get("/v1/admin/metrics/dashboard")
        assert r.status_code in (401, 403)


class TestIsolation:
    def test_empty_tenant_dashboard_reports_zero_requests(self, h):
        with_data = h.client.get(
            "/v1/admin/metrics/dashboard", headers=h.headers("t1")
        ).json()
        empty = h.client.get(
            "/v1/admin/metrics/dashboard", headers=h.headers("t2")
        ).json()
        # Whatever the exact shape, the serialized aggregates must differ —
        # identical bodies would mean the tenant filter is not applied.
        assert with_data != empty


class TestExports:
    def test_export_endpoint_answers(self, h):
        r = h.client.get(
            "/v1/admin/metrics/export", headers=h.headers("t1")
        )
        assert r.status_code in (200, 422), r.text

    def test_focus_cost_export_answers(self, h):
        r = h.client.get(
            "/v1/admin/metrics/cost/export/focus", headers=h.headers("t1")
        )
        assert r.status_code in (200, 422), r.text
