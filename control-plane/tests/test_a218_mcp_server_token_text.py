# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a218_mcp_server_token_text: `mcp_servers.auth_token` is `TEXT`.

The column holds ciphertext, which is longer than the token it wraps; at
`VARCHAR(500)` a long token no longer fitted.

Runs on the suite's SQLite; with BRUTOR_PG_TESTS=1 on Postgres, where the
revision itself is run both ways.
"""

from __future__ import annotations


import pytest
import sqlalchemy as sa

from app.core.logging_system import BrutorLogger
from app.database.models.mcp_server import MCPServer
from app.services.config_manager import ConfigManager
from app.services.oauth.encryption_service import EncryptionService
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database


def test_the_model_column_has_no_length():
    column = MCPServer.__table__.c.auth_token
    assert isinstance(column.type, sa.Text) and getattr(column.type, "length", None) is None
    assert column.nullable is True


@pytest.fixture()
def h():
    for url in throwaway_pg_database("mcp_server_token_text"):
        harness = build_admin_harness([], logger_name="mcp-server-token-text", database_url=url)
        yield harness
        harness.close()


def _server(token: str) -> MCPServer:
    return MCPServer(
        id="mcp-long", tenant_id="t1", name="long", base_url="http://127.0.0.1:9", mcp_endpoint_path="/mcp",
        auth_type="bearer", auth_token=token,
    )


def test_the_ciphertext_of_a_long_token_is_stored_whole(h):
    log = BrutorLogger("mcp-server-token-text")
    encryption = EncryptionService(config_manager=ConfigManager(logger=log, validate=False), logger=log)
    token = "t" * 480
    sealed = encryption.encrypt(token)
    assert len(sealed) > 500
    h.seed(lambda s: s.add(_server(sealed)))
    with h.Factory() as s:
        assert encryption.decrypt(s.get(MCPServer, "mcp-long").auth_token) == token
