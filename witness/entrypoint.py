# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Entrypoint of the Brutor Agent Control Witness image.

Makes sure the witness's own Postgres database exists, then runs the
unmodified upstream ``capsule-anchor`` app.

Why this exists: the package's Postgres runs its init scripts only on a
brand-new data volume, so an install upgraded in place would never get the
witness database from an init script. Creating it here (idempotently, as the
package's own DB user) works for fresh and existing volumes alike. The witness
keeps its CT log in its own database — never in the gateway's own database
(``brutor`` by default), which Alembic owns.

Successful ``GET /health`` requests are not logged: the container
healthcheck makes one every 15 s, and four identical "200 OK" lines a minute
bury the lines that matter. A health check that FAILS is still logged, and
every other request is logged as uvicorn logs it. The filter is added to uvicorn's
default logging configuration (pinned uvicorn, see ``constraints.txt``);
capsule-anchor itself is not modified.

Everything else is upstream configuration (``CAPSULE_ANCHOR_*``); see
``Dockerfile`` for provenance. Set ``BRUTOR_WITNESS_CREATE_DATABASE=0`` to skip
the step when the database is provisioned elsewhere (a managed Postgres whose
application role may not create databases).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit


def _log(msg: str) -> None:
    print(f"brutor-witness: {msg}", file=sys.stderr, flush=True)


def _ensure_database(url: str, deadline_s: float = 120.0) -> None:
    import psycopg
    from psycopg import sql

    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    if not dbname:
        return
    # Connect to the maintenance database on the same server with the same
    # credentials; CREATE DATABASE cannot run inside the target database.
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    started = time.monotonic()
    while True:
        try:
            with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
                ).fetchone()
                if exists:
                    return
                try:
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
                    _log(f"created database {dbname!r}")
                except psycopg.errors.DuplicateDatabase:
                    pass  # a concurrent replica won the race
                return
        except psycopg.OperationalError as exc:
            if time.monotonic() - started > deadline_s:
                _log(f"database server not reachable after {deadline_s:.0f}s: {exc}")
                raise
            time.sleep(2)


class _SkipSuccessfulHealthChecks(logging.Filter):
    """Drop uvicorn access lines for a 2xx ``/health``; keep everything else.

    uvicorn logs access as ``(client_addr, method, path, http_version,
    status_code)``. A record of any other shape is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        return not (path.split("?", 1)[0] == "/health" and 200 <= status < 300)


def _quiet_health_checks() -> None:
    from uvicorn.config import LOGGING_CONFIG

    # `uvicorn.run(log_config=LOGGING_CONFIG)` takes this dict by default.
    LOGGING_CONFIG.setdefault("filters", {})["skip_health"] = {
        "()": _SkipSuccessfulHealthChecks
    }
    LOGGING_CONFIG["handlers"]["access"].setdefault("filters", []).append("skip_health")


def main() -> None:
    url = (os.environ.get("CAPSULE_ANCHOR_DATABASE_URL") or "").strip()
    if url and os.environ.get("BRUTOR_WITNESS_CREATE_DATABASE", "1") != "0":
        _ensure_database(url)
    _quiet_health_checks()
    from capsule_anchor.app import main as capsule_anchor_main

    sys.exit(capsule_anchor_main())


if __name__ == "__main__":
    main()
