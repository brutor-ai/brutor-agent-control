# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Proxy Logs statistics answer when the window holds an OAuth row.

`GET /v1/admin/proxy-logs/statistics` answered 500 as soon as one row of the
OAuth proxy was inside the window, which is every installation with an MCP
server behind OAuth. Two causes, one behind the other:

* the service counted token exchanges and refreshes on
  `ProxyLog.oauth_code_exchange` and `ProxyLog.oauth_token_refresh`, columns
  the table never had (`AttributeError`). The gateway records the grant in
  `oauth_flow_type` (`authorization_code`, `refresh_token`);
* the response model's OAuth block requires `avg_duration_ms`, which the
  service never computed, and declared neither count.

The scan at the end holds every `ProxyLog.<column>` and
`ProxyMetricEntry.<column>` the code reads to the columns the models have.
It found one more: `GET /v1/admin/metrics/methods/performance` grouped by
`ProxyMetricEntry.mcp_method`, which does not exist (the method is in the
row's `mcp_data`), and answered 500 to every request.
"""

from __future__ import annotations

import ast
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.proxy_logs import create_unified_proxy_logs_admin_router
from app.core.logging_system import BrutorLogger
from app.database.models.proxy_log import ProxyLog
from app.services.proxy_log_service import UnifiedProxyLogService
from tests.admin_endpoint_harness import build_admin_harness

# (flow the gateway writes, success, duration in ms)
OAUTH_ROWS = (
    ("authorization_code", True, 100.0),
    ("authorization_code", False, 300.0),
    ("refresh_token", True, 200.0),
    (None, True, None),  # a discovery document: no grant
)


def _row(proxy_type, when, **columns):
    return ProxyLog(
        id=f"plog-{uuid.uuid4().hex[:12]}", tenant_id=columns.pop("tenant_id", "t1"), proxy_type=proxy_type,
        end_user_session_id="s", http_method="POST", endpoint="/v1/proxy/x",
        success=columns.pop("success", True), timestamp=when, created_at=when, **columns,
    )


@pytest.fixture()
def h():
    log = BrutorLogger("test-f1023")
    harness = build_admin_harness(
        [create_unified_proxy_logs_admin_router(logger=log, log_service=UnifiedProxyLogService(log))],
        logger_name="test-f1023",
    )

    def fn(s):
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        for flow, success, duration in OAUTH_ROWS:
            s.add(_row("oauth", now, oauth_flow_type=flow, success=success, duration_ms=duration))
        s.add(_row("mcp", now, mcp_method="tools/call", mcp_capability_name="search", duration_ms=50.0))
        # Outside the window, and another tenant's.
        s.add(_row("oauth", now - timedelta(days=3), oauth_flow_type="refresh_token"))
        s.add(_row("oauth", now, oauth_flow_type="refresh_token", tenant_id="t2"))

    harness.seed(fn)
    yield harness
    harness.close()


@pytest.mark.parametrize("query", ["", "?proxy_types=oauth", "?proxy_types=mcp,oauth", "?hours_ago=1"])
def test_the_statistics_answer_with_an_oauth_row_in_the_window(h, query):
    r = h.client.get(f"/v1/admin/proxy-logs/statistics{query}", headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["stats_by_type"]["oauth"] == {
        "total_requests": 4,
        "successful_requests": 3,
        "failed_requests": 1,
        "success_rate": 0.75,
        "avg_duration_ms": 200.0,
        "token_exchanges": 2,
        "token_refreshes": 1,
        "by_idp": None,
    }


def test_the_oauth_block_is_left_out_when_it_is_not_asked_for_or_empty(h):
    r = h.client.get("/v1/admin/proxy-logs/statistics?proxy_types=mcp", headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["stats_by_type"]["oauth"] is None
    assert r.json()["total_requests"] == 1
    r = h.client.get("/v1/admin/proxy-logs/statistics", headers=h.headers("t2"))
    assert r.json()["stats_by_type"]["oauth"]["total_requests"] == 1
    assert r.json()["stats_by_type"]["oauth"]["token_refreshes"] == 1


def test_the_methods_performance_route_reads_the_method_from_the_row(h):
    from app.api.v1.endpoints.admin.metrics import create_proxy_metrics_admin_router
    from app.database.models.proxy_metric import ProxyMetricEntry
    from app.services.proxy_metrics_service import UnifiedProxyMetricsService

    log = BrutorLogger("test-f1023-metrics")
    h.app.include_router(create_proxy_metrics_admin_router(UnifiedProxyMetricsService(log), log), prefix="/v1")

    def fn(s):
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        rows = (
            ("t1", {"method": "tools/call"}, True, 100.0),
            ("t1", {"method": "tools/call"}, False, 300.0),
            ("t1", {"method": "tools/list"}, True, 10.0),
            ("t1", {"tool_name": "no method"}, True, 1.0),
            ("t1", None, True, 1.0),
            ("t2", {"method": "tools/call"}, True, 1.0),
        )
        for i, (tenant, mcp_data, success, duration) in enumerate(rows):
            s.add(
                ProxyMetricEntry(
                    id=f"pm-f1023-{i}", tenant_id=tenant, timestamp=now, proxy_type="mcp", source="gateway",
                    request_id=f"r{i}", end_user_type="api_key", http_method="POST", total_duration_ms=duration,
                    success=success, request_hour=12, request_day_of_week=0, mcp_data=mcp_data,
                )
            )

    h.seed(fn)
    r = h.client.get("/v1/admin/metrics/methods/performance?period_hours=1", headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["methods"] == [
        {"method": "tools/call", "count": 2, "avg_duration_ms": 200.0, "min_duration_ms": 100.0,
         "max_duration_ms": 300.0, "success_rate": 50.0, "error_count": 1},
        {"method": "tools/list", "count": 1, "avg_duration_ms": 10.0, "min_duration_ms": 10.0,
         "max_duration_ms": 10.0, "success_rate": 100.0, "error_count": 0},
    ]


# --- the scan ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _sources():
    yield from sorted((ROOT / "app").rglob("*.py"))
    enterprise = ROOT / "enterprise" / "brutor_enterprise"
    if enterprise.is_dir():
        yield from sorted(enterprise.rglob("*.py"))


def test_the_code_reads_no_column_the_log_tables_do_not_have():
    from app.database.models.proxy_metric import ProxyMetricEntry

    models = {"ProxyLog": ProxyLog, "ProxyMetricEntry": ProxyMetricEntry}
    missing = set()
    seen = 0
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in models:
                seen += 1
                if not hasattr(models[node.value.id], node.attr):
                    missing.add((str(path.relative_to(ROOT)), f"{node.value.id}.{node.attr}"))
    assert seen > 300, seen
    assert missing == set()
