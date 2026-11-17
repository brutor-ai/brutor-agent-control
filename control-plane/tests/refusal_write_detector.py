# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Sees the one case in which the suite's shared SQLite connection behaves
unlike a server.

A server gives every database session its own connection. A request that
flushes a change and then refuses (an exception leaves the handler, the
request's session is rolled back) loses that change, whatever any other
session committed in between: the audit trail, for one, writes each record
through a session of its own and commits it.

Most of the suite runs on ONE in-memory SQLite connection shared by every
session (`StaticPool`). There, a commit by any session commits the
connection, including what the request's session had only flushed; the
rollback that follows finds nothing left to undo. A write the server would
lose is kept, and a test that reads it back passes. That is how the sign-in
lockout counter passed every test while it counted nothing on Postgres.

This module watches for exactly that sequence, on any engine whose sessions
share a connection:

    1. a session sends a change to the database (flush or DML) and does not
       commit;
    2. ANOTHER session commits on the same connection (so the change of 1
       is now durable, which a server would not have done);
    3. the session of 1 ends without committing (rollback, or close).

When it happens the test has seen a state a server would not be in. The
event is recorded (`EVENTS`), and `tests/conftest.py` fails the test that
caused it, naming the request and the tables, unless the test declares the
case with the `refusal_keeps_write` marker (the cases that are reviewed and
held on a database with real connections are listed in
`tests/test_refusal_write_semantics.py`).
"""

from __future__ import annotations

import re
import weakref
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

_DML = re.compile(r"^\s*(insert|update|delete)\b\s*(?:into|from)?\s*([\w\".]+)?", re.IGNORECASE)


@dataclass(frozen=True)
class KeptWrite:
    """One change the shared connection kept and a server would have lost."""

    tables: tuple
    request: Optional[str]

    def __str__(self) -> str:
        return f"{self.request or '(no request)'}: wrote {', '.join(self.tables) or '?'}"


#: What the current test has produced so far. `tests/conftest.py` empties it
#: before every test and reads it after.
EVENTS: List[KeptWrite] = []

#: The request the test client is sending right now ("POST /v1/..."), set by
#: the wrapper `install()` puts around `TestClient.request`.
_current_request: List[Optional[str]] = [None]


class _Open:
    """A session's uncommitted changes."""

    __slots__ = ("tables", "kept_by_another_commit", "request")

    def __init__(self) -> None:
        self.tables: Set[str] = set()
        self.kept_by_another_commit = False
        self.request: Optional[str] = None


_open: "weakref.WeakKeyDictionary[Session, _Open]" = weakref.WeakKeyDictionary()


def _shares_one_connection(session: Session) -> bool:
    try:
        bind = session.get_bind()
    except Exception:  # noqa: BLE001 — a session without a bind writes nowhere
        return False
    engine = getattr(bind, "engine", bind)
    return isinstance(getattr(engine, "pool", None), StaticPool)


def _note(session: Session, tables) -> None:
    if not _shares_one_connection(session):
        return
    entry = _open.get(session)
    if entry is None:
        entry = _open[session] = _Open()
        entry.request = _current_request[0]
    entry.tables.update(t for t in tables if t)


_PENDING = "f442_tables_about_to_be_flushed"


def _before_flush(session: Session, _ctx, _instances) -> None:
    # Asked BEFORE the flush: afterwards the unit of work no longer calls a
    # row modified (its history is reset), and an attribute set to an SQL
    # expression would be missed.
    tables = set()
    for obj in session.new:
        tables.add(getattr(obj, "__tablename__", type(obj).__name__))
    for obj in session.deleted:
        tables.add(getattr(obj, "__tablename__", type(obj).__name__))
    for obj in session.dirty:
        if session.is_modified(obj):
            tables.add(getattr(obj, "__tablename__", type(obj).__name__))
    session.info[_PENDING] = tables


def _after_flush(session: Session, _ctx) -> None:
    tables = session.info.pop(_PENDING, None)
    if tables:
        _note(session, tables)


def _do_orm_execute(state) -> None:
    if state.is_select:
        return
    statement = state.statement
    if getattr(statement, "is_dml", False):
        table = getattr(getattr(statement, "table", None), "name", None)
        _note(state.session, [table or "?"])
        return
    match = _DML.match(str(statement))
    if match:
        _note(state.session, [(match.group(2) or "?").strip('"')])


def _same_connection(a: Session, b: Session) -> bool:
    try:
        ea, eb = a.get_bind(), b.get_bind()
    except Exception:  # noqa: BLE001
        return False
    return getattr(ea, "engine", ea) is getattr(eb, "engine", eb)


def _after_commit(session: Session) -> None:
    if not _shares_one_connection(session):
        return
    _open.pop(session, None)
    for other, entry in list(_open.items()):
        if other is not session and _same_connection(other, session):
            entry.kept_by_another_commit = True


def _after_transaction_end(session: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    entry = _open.pop(session, None)
    if entry is not None and entry.kept_by_another_commit:
        EVENTS.append(KeptWrite(tuple(sorted(entry.tables)), entry.request))


_installed = False


def install() -> None:
    """Idempotent. Called by `tests/conftest.py`."""
    global _installed
    if _installed:
        return
    _installed = True
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "do_orm_execute", _do_orm_execute)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_transaction_end", _after_transaction_end)

    from starlette.testclient import TestClient

    original = TestClient.request

    def request(self, method, url, *args, **kwargs):
        previous = _current_request[0]
        _current_request[0] = f"{str(method).upper()} {str(url).split('?')[0]}"
        try:
            return original(self, method, url, *args, **kwargs)
        finally:
            _current_request[0] = previous

    TestClient.request = request
