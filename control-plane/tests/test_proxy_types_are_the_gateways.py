# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Proxy Logs filter accepts exactly the proxy types the gateway writes.

`proxy_types` was `Literal['mcp', 'llm', 'oauth', 'virtual_mcp', 'a2a']`: it
refused `skill`, which the gateway writes, and took `virtual_mcp`, which it
does not (`virtual-mcp`). A filter on skill rows answered 422 and one on
virtual servers matched nothing; the statistics counted `virtual_mcp` rows,
of which there are none.

The gateway's list is `proxy::ALL` in `brutor-gateway-core/src/constants.rs`,
read here at the gateway's committed HEAD.
"""

from __future__ import annotations

import os
import re
import typing
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.proxy_logs import create_unified_proxy_logs_admin_router
from app.api.v1.models.admin import proxy_log as models
from app.constants.proxy import PROXY_TYPES, ProxyType
from app.core.logging_system import BrutorLogger
from app.database.models.proxy_log import ProxyLog
from app.services.proxy_log_service import UnifiedProxyLogService
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source

WRITTEN = ("mcp", "virtual-mcp", "llm", "skill", "oauth", "a2a", "batch", "portal")


def _gateway_types():
    source = core_source("constants.rs").read_text(encoding="utf-8")
    module = source[source.index("pub mod proxy {"):]
    module = module[: module.index("\n}\n")]
    constants = dict(re.findall(r'pub const (\w+): &str = "([^"]+)";', module))
    listed = re.search(r"pub const ALL: &\[&str\] = &\[([^\]]+)\];", module)
    assert listed, f"proxy::ALL is gone from the gateway's constants [{core_revision()}]"
    return [constants[name.strip()] for name in listed.group(1).split(",")]


def _literal(model, field="proxy_types"):
    (inner,) = [a for a in typing.get_args(model.model_fields[field].annotation) if a is not type(None)]
    (literal,) = typing.get_args(inner)
    return typing.get_args(literal)


def test_the_filter_takes_what_the_gateway_writes():
    written = _gateway_types()
    assert tuple(written) == WRITTEN, (
        f"the gateway's proxy types changed: bring app/constants/proxy.py into line [{core_revision()}]"
    )
    assert PROXY_TYPES == WRITTEN
    assert tuple(member.value for member in ProxyType) == WRITTEN
    for model in (models.LogQueryRequest, models.StatsQueryRequest, models.ExportRequest):
        assert _literal(model) == WRITTEN, model.__name__


@pytest.fixture()
def h():
    log = BrutorLogger("test-proxy-types")
    harness = build_admin_harness(
        [create_unified_proxy_logs_admin_router(logger=log, log_service=UnifiedProxyLogService(log))],
        logger_name="test-proxy-types",
    )

    def fn(s):
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        for proxy_type in WRITTEN:
            s.add(
                ProxyLog(
                    id=f"plog-{uuid.uuid4().hex[:12]}", tenant_id="t1", proxy_type=proxy_type,
                    end_user_session_id="s", http_method="POST", endpoint="/v1/proxy/x", success=True,
                    timestamp=now, created_at=now, mcp_capability_name="tool-of-" + proxy_type,
                )
            )

    harness.seed(fn)
    yield harness
    harness.close()


@pytest.mark.parametrize("proxy_type", WRITTEN)
def test_each_type_the_gateway_writes_can_be_asked_for(h, proxy_type):
    r = h.client.post(
        "/v1/admin/proxy-logs/query", json={"hours_ago": 24, "proxy_types": [proxy_type]}, headers=h.headers()
    )
    assert r.status_code == 200, r.text
    assert [log["proxy_type"] for log in r.json()["logs"]] == [proxy_type]
    r = h.client.post(
        "/v1/admin/proxy-logs/export", json={"hours_ago": 24, "proxy_types": [proxy_type]}, headers=h.headers()
    )
    assert r.status_code == 200, r.text
    # (With an OAuth row in the window the statistics answered 500:
    # tests/test_oauth_statistics.py.)
    r = h.client.get(f"/v1/admin/proxy-logs/statistics?proxy_types={proxy_type}", headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["total_requests"] == 1


@pytest.mark.parametrize("proxy_type", ["virtual_mcp", "skills", "MCP", ""])
def test_a_type_the_gateway_does_not_write_is_refused(h, proxy_type):
    r = h.client.post(
        "/v1/admin/proxy-logs/query", json={"hours_ago": 24, "proxy_types": [proxy_type]}, headers=h.headers()
    )
    assert r.status_code == 422, r.text
    r = h.client.post(
        "/v1/admin/proxy-logs/export", json={"hours_ago": 24, "proxy_types": [proxy_type]}, headers=h.headers()
    )
    assert r.status_code == 422, r.text
    if proxy_type:
        # The statistics route takes a comma-separated list.
        r = h.client.get(f"/v1/admin/proxy-logs/statistics?proxy_types=mcp,{proxy_type}", headers=h.headers())
        assert r.status_code == 422, r.text
        assert proxy_type in r.json()["detail"]


def test_virtual_server_rows_are_counted_and_shown_as_mcp_rows(h):
    r = h.client.get("/v1/admin/proxy-logs/statistics?hours_ago=24&proxy_types=mcp,virtual-mcp", headers=h.headers())
    assert r.status_code == 200, r.text
    by_type = r.json()["stats_by_type"]
    assert by_type["virtual_mcp"] and by_type["virtual_mcp"]["total_requests"] == 1
    assert by_type["mcp"]["total_requests"] == 1
    # The virtual server's row carries its MCP block, as an MCP row does.
    r = h.client.post(
        "/v1/admin/proxy-logs/query", json={"hours_ago": 24, "proxy_types": ["virtual-mcp"]}, headers=h.headers()
    )
    (log,) = r.json()["logs"]
    assert log["mcp"]["capability_name"] == "tool-of-virtual-mcp"
