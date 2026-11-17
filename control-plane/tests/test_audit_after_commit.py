# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A success audit record exists only for a write that was committed.

The audit trail writes each record through a session of its own and commits
it at once. For a refusal that is the point: the record survives the
rollback of the request. For a success it was wrong: about 340 handlers
record the success before their own commit, so a request that failed after
that point (the commit itself, a later step of the handler) left an audit
entry for a write that was rolled back.

The rule, in one place (`UnifiedAdminAuditLogger.log`): a record with
`success=True`, logged on a session that holds uncommitted writes, is queued
on that session and written when the session's root transaction commits, in
the order it was logged; it is dropped when the transaction rolls back or the
session is closed without a commit. A record with `success=False`, and a
success logged on a session with nothing uncommitted (a read, or a handler
that already committed), is written at once, as before.

Held through real routes on a database with a connection per session: a
SQLite file, or Postgres with BRUTOR_PG_TESTS=1.

What each database shows of the defect: on Postgres the commit-failure case
kept the audit row. On a SQLite file the audit trail's own connection could
not write while the request held the file, so the record of a success was
never written at all: there it is the success cases that failed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdmin, TenantAdminAuditLog, TenantRole
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database


@pytest.fixture()
def h(tmp_path):
    for url in throwaway_pg_database("audit_after_commit"):
        log = BrutorLogger("audit-after-commit")
        manager = LLModelsManager(logger=log, encryption_service=None)
        harness = build_admin_harness(
            [create_llm_models_router(manager, log, encryption_service=None)],
            logger_name="audit-after-commit",
            database_url=url or f"sqlite:///{tmp_path / 'audit.db'}?timeout=0.05",
        )
        harness.seed(lambda s: s.add(LlmProvider(id="prov-t1", tenant_id="t1", key="openai", display_name="OpenAI")))
        yield harness
        harness.close()


def _audit(h, *actions):
    s = h.Factory()
    try:
        rows = (
            s.query(TenantAdminAuditLog)
            .filter(TenantAdminAuditLog.action.in_(actions))
            .order_by(TenantAdminAuditLog.timestamp.asc())
            .all()
        )
        return [(r.action, r.success, r.resource_id) for r in rows]
    finally:
        s.close()


def _models(h):
    s = h.Factory()
    try:
        return sorted(m.name for m in s.query(LLModel).all())
    finally:
        s.close()


def _body(name="m1"):
    return {"name": name, "provider_id": "prov-t1", "model_name": "gpt-4o-mini", "mode": "chat", "enabled": True}


class CommitFailed(RuntimeError):
    pass


def _fail_commits_that_write(h):
    """Make the commit of a request session that holds a new model fail, as
    a lost connection or a deferred constraint would. Sessions of the
    harness's factory only: the audit trail's own session is not one."""

    def boom(session):
        if any(isinstance(obj, LLModel) for obj in session.identity_map.values()):
            raise CommitFailed("the commit failed")

    event.listen(h.Factory, "before_commit", boom)
    return lambda: event.remove(h.Factory, "before_commit", boom)


# ---------------------------------------------------------------------------
# Through real routes
# ---------------------------------------------------------------------------


def test_a_route_whose_commit_fails_leaves_no_success_audit_row(h):
    headers = h.headers("t1")
    restore = _fail_commits_that_write(h)
    try:
        with pytest.raises(CommitFailed):
            h.client.post("/v1/admin/llms", json=_body(), headers=headers)
    finally:
        restore()
    assert _models(h) == []
    # It recorded `llm_create ... success` for a model that does not exist.
    assert _audit(h, "llm_create") == []


def test_a_successful_route_leaves_exactly_one(h):
    r = h.client.post("/v1/admin/llms", json=_body(), headers=h.headers("t1"))
    assert r.status_code == 201, r.text
    assert _models(h) == ["m1"]
    assert _audit(h, "llm_create") == [("llm_create", True, r.json()["model"]["id"])]


def test_successive_requests_are_recorded_in_their_order(h):
    headers = h.headers("t1")
    first = h.client.post("/v1/admin/llms", json=_body("m1"), headers=headers).json()["model"]["id"]
    second = h.client.post("/v1/admin/llms", json=_body("m2"), headers=headers).json()["model"]["id"]
    assert h.client.patch(f"/v1/admin/llms/{first}", json={"description": "d"}, headers=headers).status_code == 200
    assert h.client.delete(f"/v1/admin/llms/{second}", headers=headers).status_code == 204
    assert h.client.post(f"/v1/admin/llms/{first}/copy", headers=headers).status_code in (200, 201)
    recorded = _audit(h, "llm_create", "llm_update", "llm_delete", "llm_model_copied")
    assert [(action, success) for action, success, _id in recorded] == [
        ("llm_create", True),
        ("llm_create", True),
        ("llm_update", True),
        ("llm_delete", True),
        ("llm_model_copied", True),
    ]
    assert [rid for _a, _s, rid in recorded[:4]] == [first, second, first, second]


def test_a_refused_routes_denial_record_is_kept(h):
    reader = h.limited_headers("t1", [Permission.AI_MODEL_READ], username="reader_t1")
    r = h.client.post("/v1/admin/llms", json=_body(), headers=reader)
    assert r.status_code == 403
    assert _models(h) == []
    denied = _audit(h, f"unauthorized_access_{Permission.AI_MODEL_CREATE.value}")
    assert denied == [(f"unauthorized_access_{Permission.AI_MODEL_CREATE.value}", False, Permission.AI_MODEL_CREATE.value)]


def test_a_commit_that_fails_after_a_success_keeps_the_earlier_requests_records(h):
    headers = h.headers("t1")
    assert h.client.post("/v1/admin/llms", json=_body("kept"), headers=headers).status_code == 201
    restore = _fail_commits_that_write(h)
    try:
        with pytest.raises(CommitFailed):
            h.client.post("/v1/admin/llms", json=_body("lost"), headers=headers)
    finally:
        restore()
    assert _models(h) == ["kept"]
    assert [a for a, _s, _r in _audit(h, "llm_create")] == ["llm_create"]


# ---------------------------------------------------------------------------
# The rule itself, on the logger
# ---------------------------------------------------------------------------


def _admin(s):
    return s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").one()


def _role(name):
    return TenantRole(id=f"role-{name}", tenant_id="t1", name=name, description="probe", is_system=False)


def _log(s, action, **kw):
    return UnifiedAdminAuditLogger().log(action=action, db=s, admin=_admin(s), admin_type="tenant", tenant_id="t1", **kw)


def _seen_by_another_connection(h, action):
    return len(_audit(h, action))


def test_a_success_on_uncommitted_writes_is_written_at_the_commit_not_before(h):
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        _log(s, "f541.success")
        assert _seen_by_another_connection(h, "f541.success") == 0
        s.commit()
        assert _seen_by_another_connection(h, "f541.success") == 1
        # Written once: a later commit of the same session adds nothing.
        s.add(_role("b"))
        s.commit()
        assert _seen_by_another_connection(h, "f541.success") == 1


def test_a_rollback_drops_it_and_a_later_commit_does_not_bring_it_back(h):
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        _log(s, "f541.rolled-back")
        s.rollback()
        s.add(_role("b"))
        s.commit()
    assert _seen_by_another_connection(h, "f541.rolled-back") == 0


def test_closing_without_a_commit_drops_it(h):
    s = Session(bind=h.engine)
    s.add(_role("a"))
    s.flush()
    _log(s, "f541.closed")
    s.close()
    assert _seen_by_another_connection(h, "f541.closed") == 0


def test_an_unflushed_write_counts_as_uncommitted(h):
    with Session(bind=h.engine, autoflush=False) as s:
        admin = _admin(s)
        s.add(_role("a"))
        UnifiedAdminAuditLogger().log(action="f541.unflushed", db=s, admin=admin, admin_type="tenant", tenant_id="t1")
        assert _seen_by_another_connection(h, "f541.unflushed") == 0
        s.commit()
    assert _seen_by_another_connection(h, "f541.unflushed") == 1


def test_a_success_with_nothing_uncommitted_is_written_at_once(h):
    # A read, or a handler that committed before it logged.
    with Session(bind=h.engine) as s:
        _log(s, "f541.read")
        assert _seen_by_another_connection(h, "f541.read") == 1
        s.add(_role("a"))
        s.commit()
        _log(s, "f541.after-commit")
        assert _seen_by_another_connection(h, "f541.after-commit") == 1
        s.rollback()
    assert _seen_by_another_connection(h, "f541.read") == 1
    assert _seen_by_another_connection(h, "f541.after-commit") == 1


def test_a_refusal_is_written_at_once_and_survives_the_rollback(h):
    with Session(bind=h.engine) as s:
        _log(s, "f541.denied", success=False)
        assert _seen_by_another_connection(h, "f541.denied") == 1
        s.rollback()
    assert _audit(h, "f541.denied") == [("f541.denied", False, None)]


def test_a_refusal_on_uncommitted_writes_is_kept_and_the_writes_are_not(h):
    if h.engine.dialect.name != "postgresql":
        pytest.skip(
            "a SQLite file is locked whole by the request's open transaction: the audit trail's "
            "connection cannot write there (see tests/test_refusal_write_semantics.py)"
        )
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        _log(s, "f541.success-then-refused")
        _log(s, "f541.denied-after-flush", success=False)
        s.rollback()
    assert _seen_by_another_connection(h, "f541.denied-after-flush") == 1
    assert _seen_by_another_connection(h, "f541.success-then-refused") == 0
    with h.Factory() as s:
        assert s.get(TenantRole, "role-a") is None


def test_queued_records_are_written_in_the_order_they_were_logged(h):
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        for n in range(5):
            _log(s, "f541.ordered", resource_id=f"r{n}")
        s.commit()
    assert [rid for _a, _s, rid in _audit(h, "f541.ordered")] == ["r0", "r1", "r2", "r3", "r4"]


def test_the_queue_is_handed_to_the_trail_once_in_that_order(h, monkeypatch):
    # The rows carry the time they were logged, which is what a reader sorts
    # by; this holds the order they are inserted in as well.
    from app.core.admin_user.services import audit_logger

    written = []
    real = audit_logger._write

    def recording(bind, entries):
        written.append([entry.resource_id for entry in entries])
        real(bind, entries)

    monkeypatch.setattr(audit_logger, "_write", recording)
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        for n in range(4):
            _log(s, "f541.batch", resource_id=f"r{n}")
        s.commit()
    assert written == [["r0", "r1", "r2", "r3"]]


def test_a_savepoint_release_writes_nothing_the_root_commit_does(h):
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        with s.begin_nested():
            s.add(_role("b"))
            _log(s, "f541.nested")
        assert _seen_by_another_connection(h, "f541.nested") == 0
        s.commit()
    assert _seen_by_another_connection(h, "f541.nested") == 1


def test_the_id_returned_is_the_records(h):
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        audit_id = _log(s, "f541.id")
        s.commit()
    with h.Factory() as s:
        assert [r.id for r in s.query(TenantAdminAuditLog).filter_by(action="f541.id")] == [audit_id]


def test_a_record_that_cannot_be_written_at_the_commit_does_not_fail_the_commit(h, monkeypatch):
    # As before the change: the audit trail logs its own failure and the
    # caller goes on. The write is committed either way.
    with Session(bind=h.engine) as s:
        s.add(_role("a"))
        s.flush()
        _log(s, "f541.unwritable")

        def broken(*_a, **_k):
            raise RuntimeError("audit table unreachable")

        monkeypatch.setattr("app.core.admin_user.services.audit_logger._write", broken)
        s.commit()
    with h.Factory() as s:
        assert s.get(TenantRole, "role-a") is not None
    assert _seen_by_another_connection(h, "f541.unwritable") == 0
