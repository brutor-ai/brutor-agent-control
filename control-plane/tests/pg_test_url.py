# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The one place a test reads ``BRUTOR_PG_TEST_URL``.

Default: the local server's ``postgres`` database. Tests that need a schema
create a scratch database on that server (``throwaway_pg_database``); a test
that writes straight into the URL's database finds no tables there and fails
loudly, which is the point.

Never the live ``ai_gateway``: a suite run pointed at it drops and
re-initialises the demo database. ``ai_gateway`` is refused unless it is CI's
throwaway Postgres service (``CI=true``) or the caller deliberately runs ONE
tenant-scoped file against the dev database (``BRUTOR_PG_ALLOW_LIVE_DB=1``,
after a ``pg_dump``).
"""

from __future__ import annotations

import os

DEFAULT_PG_TEST_URL = "postgresql://brutor:brutor123@127.0.0.1:1/postgres"
LIVE_DB = "ai_gateway"
LOWEST_EPHEMERAL_PORT = 32768


def assert_not_live(url: str) -> str:
    from sqlalchemy.engine import make_url

    name = make_url(url).database
    allowed = os.environ.get("CI") == "true" or os.environ.get("BRUTOR_PG_ALLOW_LIVE_DB") == "1"
    if name == LIVE_DB and not allowed:
        raise RuntimeError(
            f"refusing to run tests against `{LIVE_DB}` (the live dev database). Point "
            "BRUTOR_PG_TEST_URL at .../postgres (scratch databases are created there) or a "
            "database made for the run; BRUTOR_PG_ALLOW_LIVE_DB=1 only for one tenant-scoped "
            "file, after a pg_dump."
        )
    return url


def assert_port_cannot_self_connect(url: str) -> str:
    """Refuse an ephemeral port: a client can occasionally connect to itself there.

    With nothing listening on the address tried, the kernel can pick source port
    = destination port. The client then reads its own first message, and an
    unrelated test fails on a stray NUL or EINVAL, once in thousands of connects.
    macOS's range starts at 49152, Linux's at 32768. Same rule as core's
    ``tests/test_harness_guard.rs``.
    """
    from sqlalchemy.engine import make_url

    port = make_url(url).port
    if port is not None and port >= LOWEST_EPHEMERAL_PORT:
        raise RuntimeError(
            f"refusing test database port {port}: it is in the ephemeral range and a "
            f"connection can reach itself there. Use a port below {LOWEST_EPHEMERAL_PORT}."
        )
    return url


def pg_test_url() -> str:
    """``BRUTOR_PG_TEST_URL``, else the local ``postgres`` database; never live."""
    return assert_port_cannot_self_connect(
        assert_not_live(os.environ.get("BRUTOR_PG_TEST_URL", DEFAULT_PG_TEST_URL))
    )
