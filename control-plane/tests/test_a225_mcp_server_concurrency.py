# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a225_mcp_server_concurrency: `mcp_servers.max_concurrent_requests` is NULL (the gateway reads 10) or
a positive whole number, and the database refuses anything else.

Runs on the suite's SQLite; with BRUTOR_PG_TESTS=1 on Postgres, where the
revision itself is run both ways.
"""

from __future__ import annotations


import pytest
import sqlalchemy as sa

from app.database.models.mcp_server import MCPServer
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database


@pytest.fixture()
def h():
    for url in throwaway_pg_database("mcp_server_concurrency"):
        harness = build_admin_harness([], logger_name="mcp-server-concurrency", database_url=url)
        yield harness
        harness.close()


def _server(sid: str, value) -> MCPServer:
    return MCPServer(
        id=sid, tenant_id="t1", name=sid, base_url=f"http://{sid}.internal", mcp_endpoint_path="/mcp",
        max_concurrent_requests=value,
    )


def test_unset_is_null_and_a_positive_number_is_kept(h):
    h.seed(lambda s: s.add_all([_server("unset", None), _server("four", 4)]))
    with h.Factory() as s:
        assert s.get(MCPServer, "unset").max_concurrent_requests is None
        assert s.get(MCPServer, "four").max_concurrent_requests == 4


@pytest.mark.parametrize("value", [0, -1])
def test_the_database_refuses_a_value_that_is_not_positive(h, value):
    with pytest.raises(sa.exc.IntegrityError):
        h.seed(lambda s: s.add(_server("bad", value)))
