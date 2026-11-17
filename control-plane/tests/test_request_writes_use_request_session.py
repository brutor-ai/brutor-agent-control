# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Request handlers write through the request's own session.

Two request-path guarantees depend on it:
* `CommitBeforeResponseMiddleware` commits the request session's writes
  before the response starts, so a failed commit is a 500, not a silent 200;
* `invalidate_proxy_cache` defers its push until that session commits, so a
  gateway can never reload the old row in the gap.

Both watch only the session `get_db` bound to the request. A handler that
opens its own `SessionLocal()`, an `engine.connect()` / `engine.begin()`, or
a raw DB-API connection writes outside both — its commit timing and any
invalidation alongside it fall back to the old, racy behaviour. No handler
does that for gateway configuration today; this keeps it that way.
"""

from __future__ import annotations

import re
from pathlib import Path
from tests.source_trees import python_sources

API = Path(__file__).resolve().parents[1] / "app" / "api"

#: file (relative to app/api) → why its separate session is allowed.
ALLOWED = {
    "v1/endpoints/admin/system_logs_ws.py": (
        "WebSocket upgrade, not a get_db request: the ticket is consumed and "
        "the session checked on a session of its own, committed there; no "
        "gateway configuration is written."
    ),
}

OUTSIDE = re.compile(
    r"\bSessionLocal\s*\(\s*\)|\bengine\.(?:connect|begin)\s*\(|\braw_connection\s*\(|\.connection\(\)\.connection\b"
)


def _violations(rel: str, text: str) -> list[str]:
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    return [f"{rel}: {m.group(0)}" for m in OUTSIDE.finditer(code)]


def test_handlers_do_not_write_outside_the_request_session():
    bad = []
    for path in python_sources(API):
        rel = path.relative_to(API).as_posix() if path.is_relative_to(API) else path.relative_to(API.parents[1]).as_posix()
        if rel in ALLOWED:
            continue
        bad += _violations(rel, path.read_text())
    assert not bad, (
        "use the request's `db` (Depends(get_db)); a separate session or raw "
        "connection bypasses commit-before-response and deferred cache "
        "invalidation:\n" + "\n".join(bad)
    )


def test_every_allowance_still_exists_and_says_why():
    for rel, why in ALLOWED.items():
        assert (API / rel).exists(), f"stale allowance: {rel}"
        assert len(why) > 40


def test_the_guard_catches_the_shapes_it_forbids():
    for shape in (
        "db = SessionLocal()\n",
        "with engine.connect() as conn:\n",
        "with engine.begin() as conn:\n",
        "conn = engine.raw_connection()\n",
        "raw = db.connection().connection\n",
    ):
        assert _violations("x.py", shape), shape
    assert not _violations("x.py", "# db = SessionLocal()\n")
