# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Authentication never locks the admin session row for the length of a request.

The bug class: bookkeeping written inside the request's transaction. The
`last_activity` touch in `verify_token` was an UPDATE flushed in the request's
own transaction, so the session row stayed locked until the request finished.
Requests of the same session queued behind the slowest one; one that reached
the statement timeout failed inside `verify_token`, got a 401, and the console
signed the admin out.

Guards:
  * `verify_token` leaves nothing to write in the request's session;
  * a stale session is touched once, a fresh one not at all, and a touch that
    cannot be made never fails authentication;
  * on Postgres, a session row locked by another transaction does not delay
    or fail `verify_token` (BRUTOR_PG_TESTS=1);
  * no module but `app/services/session_activity.py` writes `last_activity`
    on a session (creation at login aside).
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import jwt
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.database import Base
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.tenant import Tenant
from app.services import auth_read_model, session_activity
from app.services.session_activity import (
    ACTIVITY_TOUCH_INTERVAL_SECONDS,
    touch_session_activity,
)
from tests.admin_endpoint_harness import throwaway_pg_database
from tests.test_unified_auth_service import _mk_tenant, _mk_tenant_admin_role
from tests.source_trees import python_sources, source_label

JWT_SECRET = "test-jwt-secret-not-for-production"
TABLES = [
    Tenant.__table__, TenantRole.__table__, TenantRolePermission.__table__,
    SystemAdmin.__table__, TenantAdmin.__table__,
    SystemAdminSession.__table__, TenantAdminSession.__table__,
]
STALE = timedelta(seconds=ACTIVITY_TOUCH_INTERVAL_SECONDS * 2)


def _with_referenced(tables):
    """The tables plus everything their foreign keys point at, transitively:
    Postgres refuses a table whose referenced table is missing (SQLite does
    not check)."""
    import app.database.models  # noqa: F401 — registers every table on Base.metadata

    seen, todo = {}, list(tables)
    while todo:
        t = todo.pop()
        if t.name in seen:
            continue
        seen[t.name] = t
        todo.extend(fk.column.table for fk in t.foreign_keys)
    return list(seen.values())


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


@pytest.fixture()
def svc():
    return UnifiedAuthService(jwt_secret=JWT_SECRET, jwt_expiry_hours=8)


@pytest.fixture()
def factory(tmp_path):
    # A file database: every connection is a real, separate connection, so a
    # write the request's session did not commit is visible as such.
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}", future=True)
    Base.metadata.create_all(bind=engine, tables=TABLES)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _jti(token):
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["jti"]


def _set_last_activity(Factory, model, sid, when):
    db = Factory()
    try:
        db.get(model, sid).last_activity = when
        db.commit()
    finally:
        db.close()


def _last_activity(Factory, model, sid):
    db = Factory()
    try:
        return _aware(db.get(model, sid).last_activity)
    finally:
        db.close()


def _verify(svc, Factory, token):
    """verify_token the way a request does: a fresh session, the read model
    not vouching for it. Returns (result, the still-open request session)."""
    auth_read_model.forget_session(_jti(token))
    db = Factory()
    return svc.verify_token(token, db=db), db


# ---------------------------------------------------------------------------
# The incident itself, on Postgres
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_url():
    yield from throwaway_pg_database("session_activity")


# ---------------------------------------------------------------------------
# One writer
# ---------------------------------------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app"
OWNER = APP / "services" / "session_activity.py"
#: `x.last_activity = …` (an ORM write) or the old method. Keyword arguments at
#: session creation (`last_activity=now` inside a constructor call) are inserts
#: of a new row and lock nothing.
WRITES = re.compile(r"\.last_activity\s*=[^=]|\bupdate_activity\s*\(")


def test_only_the_activity_module_writes_last_activity():
    found = []
    for path in python_sources(APP):
        if path == OWNER:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if WRITES.search(line):
                found.append(f"{source_label(path)}:{n}: {line.strip()}")
    assert not found, (
        "Record session activity with app.services.session_activity.touch_session_activity "
        "(its own transaction), never in the request's:\n  " + "\n  ".join(found)
    )


def test_the_guard_catches_the_old_shapes():
    assert WRITES.search("    session.last_activity = now")
    assert WRITES.search("    session.update_activity()")
    assert not WRITES.search("    last_activity=datetime.now(timezone.utc),")
    assert not WRITES.search("    if row.last_activity == cutoff:")
