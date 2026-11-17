# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The edition guard and reset, end to end on Postgres: real installs
(`init_database()`), the real command (`python main.py edition-reset`), each
in a process of its own, as an operator runs them.

Here, what needs the Community Edition alone: a Community install starts on
an empty database, writes no edition marker and no Enterprise row, starts
again on what it wrote itself, and the reset command finds nothing to reset.

What needs an Enterprise install (the marker it writes, a Community start
refused on its database, the reset, the marker's migration) is the Enterprise
half, `enterprise/tests/test_edition_install_pg.py`, which uses the helpers
below.

Skipped unless `BRUTOR_PG_TESTS=1`. Every database is created by this module
on `BRUTOR_PG_TEST_URL`'s server (named `brutor_control_d15_...`, made from
`template0`) and dropped afterwards. The reset is only ever run against those.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.edition_compositions import (
    PG_DATABASE_PREFIX,
    count,
    pg_enabled,
    scratch_pg_database,
)

pytestmark = pytest.mark.skipif(not pg_enabled(), reason="needs Postgres; set BRUTOR_PG_TESTS=1")

REPO = Path(__file__).resolve().parents[1]

COMMUNITY_START = """
    import app.database as db
    import app.database.models  # noqa: F401
    from tests.community_process import become_community_process
    become_community_process()
    db.init_database()
    print("INIT_OK")
"""

def _env(url: str) -> dict:
    assert PG_DATABASE_PREFIX in url
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "JWT_SECRET": os.environ.get("JWT_SECRET", "x" * 48),
        "ENCRYPTION_KEY": os.environ.get("ENCRYPTION_KEY", "a" * 44 + "="),
        # A private temp dir: `init_database` skips a second pass under the
        # same parent PID (its sibling-worker sentinel lives there).
        "TMPDIR": tempfile.mkdtemp(prefix="d15-"),
        "PYTHONPATH": str(REPO),
        "BRUTOR_VERSION": "9.9.9",
        # As both packages do: the platform service key is configured (none
        # is generated), and the install registers it.
        "BRUTOR_GATEWAY_SERVICE_API_KEY": os.environ.get("BRUTOR_GATEWAY_SERVICE_API_KEY") or "sk_brutor_api_" + "t" * 40,
    }
    env.pop("REDIS_URL", None)
    return env


def _python(script: str, url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPO,
        env=_env(url),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _popen(script: str, url: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=REPO,
        env=_env(url),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# The real command, as `python main.py edition-reset ...` runs it, except
# that the developer's `.env` (which the entry point would load from beside
# the sources) is not part of what is tested.
_MAIN = (
    "import dotenv, runpy, sys\n"
    "dotenv.find_dotenv = lambda *a, **k: ''\n"
    "dotenv.load_dotenv = lambda *a, **k: False\n"
    "sys.argv = ['main.py', *sys.argv[1:]]\n"
    "runpy.run_path('main.py', run_name='__main__')\n"
)


def _command(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _MAIN, "edition-reset", *args],
        cwd=REPO,
        env=_env(url),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _started(proc) -> bool:
    return proc.returncode == 0 and "INIT_OK" in proc.stdout


def _output(proc) -> str:
    return f"stdout:\n{proc.stdout[-3000:]}\n\nstderr:\n{proc.stderr[-6000:]}"


def _marker(url: str):
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT enterprise_first_started_at, enterprise_last_started_at, "
                    "enterprise_first_version, enterprise_last_version FROM edition_marker"
                )
            ).all()
    finally:
        engine.dispose()


def _counts(url: str, tables, where: str = "") -> dict:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return {table: count(conn, table, where or None) for table in tables}
    finally:
        engine.dispose()


# Tables the default tenant of an install has rows in, in both editions.
COMMUNITY_TABLES = (
    "tenant_roles",
    "tenant_role_permissions",
    "resource_groups",
    "mcp_servers",
    "agent_skills",
    "guardrail_configs",
    "llm_providers",
    "api_keys",
    "compliance_frameworks",
)


# ---------------------------------------------------------------------------
# Community
# ---------------------------------------------------------------------------


class TestACommunityInstall:
    @pytest.fixture(scope="class")
    def community_url(self):
        with scratch_pg_database() as url:
            proc = _python(COMMUNITY_START, url)
            assert _started(proc), _output(proc)
            yield url

    def test_it_starts_on_an_empty_database_and_writes_no_marker(self, community_url):
        assert _marker(community_url) == []
        assert _counts(community_url, ["tenants"]) == {"tenants": 1}

    def test_it_leaves_every_enterprise_table_empty(self, community_url):
        from app.composition.edition import enterprise_only_tables

        tables = [table for table, _ in enterprise_only_tables()]
        assert {t: n for t, n in _counts(community_url, tables).items() if n} == {}

    def test_it_installs_the_community_estate(self, community_url):
        counts = _counts(community_url, COMMUNITY_TABLES + ("tenant_admins",))
        assert all(counts.values()), counts

    def test_it_starts_again_on_what_it_wrote(self, community_url):
        """The check that matters most for a Community operator: the edition
        does not refuse its own database."""
        proc = _python(COMMUNITY_START, community_url)
        assert _started(proc), _output(proc)
        assert _marker(community_url) == []

    def test_the_command_finds_nothing_to_reset(self, community_url):
        before = _counts(community_url, COMMUNITY_TABLES + ("tenant_admin_audit_logs",))
        proc = _command(community_url, "--confirm-delete-enterprise-data")
        assert proc.returncode == 0, _output(proc)
        assert "Nothing to reset" in proc.stdout
        assert _counts(community_url, COMMUNITY_TABLES + ("tenant_admin_audit_logs",)) == before
