# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
No test may point a database URL at a server that could be real.

The placeholder DATABASE_URL was `postgresql://test:test@localhost/...`. On a
developer machine localhost:5432 is the live stack's Postgres, and a pytest
run did connect there (refused only because no `test` role exists).
Placeholders now use 127.0.0.1:1, which refuses at once.
Throwaway servers for Postgres-backed tests come from BRUTOR_PG_TEST_URL
(`tests/pg_test_url.py`), never a literal here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from tests.pg_test_url import DEFAULT_PG_TEST_URL, assert_port_cannot_self_connect, pg_test_url
from tests.source_trees import test_label, test_sources

TESTS = Path(__file__).resolve().parent
LITERAL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\"'\s]+")


def test_the_session_placeholder_refuses_connections():
    url = make_url(os.environ["DATABASE_URL"])
    assert (url.host, url.port) == ("127.0.0.1", 1), str(url)


def test_no_test_file_hardcodes_a_reachable_database():
    bad = []
    for path in test_sources():
        if path.name == Path(__file__).name:
            continue
        for m in LITERAL.finditer(path.read_text()):
            try:
                url = make_url(m.group(0).rstrip(",)"))
            except Exception:
                continue
            host, port = url.host or "", url.port or 5432
            # Hosts a test process can reach; a compose service name (e.g.
            # "postgres") only resolves inside the Docker network.
            if host in ("localhost", "127.0.0.1", "::1") and port == 5432:
                bad.append(f"{test_label(path)}: {m.group(0)}")
    assert not bad, "a test points at a database on the default port:\n" + "\n".join(bad)


def test_the_guard_catches_the_old_placeholder():
    url = make_url("postgresql://test:test@localhost/test_brutor")
    assert (url.host, url.port or 5432) == ("localhost", 5432)


def test_the_test_database_port_cannot_connect_to_itself():
    # Fails the run early and by name, not as a rare stray NUL in another test.
    assert pg_test_url()


@pytest.mark.parametrize("port", [32768, 49152, 55437, 55500, 65535])
def test_the_port_guard_refuses_the_ephemeral_range(port):
    with pytest.raises(RuntimeError, match="ephemeral"):
        assert_port_cannot_self_connect(f"postgresql://u:p@localhost:{port}/postgres")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost/postgres",
        "postgresql://u:p@localhost:5432/postgres",
        "postgresql://u:p@127.0.0.1:15433/postgres",
        "postgresql://u:p@127.0.0.1:32767/postgres",
        DEFAULT_PG_TEST_URL,
    ],
)
def test_the_port_guard_allows_ports_below_it(url):
    assert assert_port_cannot_self_connect(url) == url


# A suite that writes rows into BRUTOR_PG_TEST_URL's own database finds no tables
# there, so it errors on every local run; and its docs then suggested the live
# database as the way out. 77 tests in two files sat like this, unnoticed because
# they skip unless BRUTOR_PG_TESTS=1.
DIRECT_USE = re.compile(r"create_engine\(\s*pg_test_url\(\)|^\s*url\s*=\s*pg_test_url\(\)", re.M)
# Pinned to one revision, this fails the moment a later migration lands.
HEAD_PIN = re.compile(r"alembic_version[\"']?\)\)\.scalar\(\)\s*==\s*\(?\s*[\"']a\d")


def _test_sources():
    me = Path(__file__).name
    return [(p.name, p.read_text()) for p in test_sources() if p.name not in (me, "pg_test_url.py")]


def test_no_suite_writes_straight_into_the_pg_test_url_database():
    offenders = [name for name, src in _test_sources() if DIRECT_USE.search(src)]
    assert not offenders, f"use throwaway_installed_pg_database (admin_endpoint_harness): {offenders}"


def test_no_migration_test_pins_the_head_revision():
    offenders = [name for name, src in _test_sources() if HEAD_PIN.search(src)]
    assert not offenders, f"check against the head, not one revision: {offenders}"


def test_the_scanners_catch_the_shapes_that_failed():
    assert DIRECT_USE.search("    url = pg_test_url()\n    engine = create_engine(url)")
    assert DIRECT_USE.search("engine = create_engine(pg_test_url())")
    # Connecting to the server to create a scratch database is the right use.
    assert not DIRECT_USE.search("    admin = make_url(pg_test_url())")
    assert HEAD_PIN.search('text("SELECT version_num FROM alembic_version")).scalar() == (\n "a203_x"')
