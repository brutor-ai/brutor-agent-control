# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A proxy-cache invalidation must reach the proxies only AFTER the change it
announces is committed.

Admin handlers call `invalidate_proxy_cache(...)` inside the handler, but the
request's transaction is committed by the `get_db` dependency afterwards.
Publishing straight away opened a window: the core evicts on the message, a
proxy request in the gap reloads the OLD row from Postgres and caches it until
TTL — so a tightened limit, a revoked grant or a guardrail change applied
late. The fix is in one place: an invalidation raised while the request's
session holds uncommitted writes is queued on that session and published from
its commit (dropped on rollback); `CommitBeforeResponseMiddleware` commits any
session with writes as the response starts, so the publish — and the Warning header on
a failed publish — still happen before the client sees the response.

FastAPI note (pinned below): in the FastAPI in use, the teardown of a
request-scoped yield dependency (`get_db`'s `db.commit()`) runs AFTER the
response has been sent, so headers could not be set from that commit.

Real `get_db`, a SQLite file database, and a fake Redis that reads the row from
a SEPARATE connection at the moment it is called. No Postgres, no Redis.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, create_engine, event, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

import app.database as database
from app.core.sync_bridge import block_on
from app.database import get_db
from app.middleware.cache_invalidation import (
    CacheInvalidationWarningMiddleware,
    CommitBeforeResponseMiddleware,
)
from app.services import cache_invalidation as ci
from app.services.cache_invalidation import invalidate_proxy_cache

_Base = declarative_base()


class _Widget(_Base):
    __tablename__ = "ci_ordering_widget"
    id = Column(String, primary_key=True)
    limit_value = Column(Integer, nullable=False)


class _RecordingRedis:
    """Fake publisher: records every message together with what a separate
    connection can see of the row at that instant."""

    def __init__(self, engine, events):
        self.engine = engine
        self.events = events
        self.published = []

    async def publish(self, channel, message):
        with self.engine.connect() as conn:
            seen = conn.execute(
                text("SELECT limit_value FROM ci_ordering_widget WHERE id = 'w1'")
            ).scalar()
        self.events.append("publish")
        self.published.append((channel, json.loads(message), seen))


class _DeadRedis:
    async def publish(self, channel, message):
        raise ConnectionError("Timeout connecting to server")


class _PassThrough(BaseHTTPMiddleware):
    """Stands in for Tenant/Maintenance/CORS: a task boundary between the
    outer warning middleware and the router, as in the real app."""

    async def dispatch(self, request, call_next):
        return await call_next(request)


@pytest.fixture(autouse=True)
def _reset_publisher_health():
    ci._publisher_health.reset()
    yield
    ci._publisher_health.reset()


@pytest.fixture
def alerts(monkeypatch):
    raised = []
    monkeypatch.setattr(
        ci, "_raise_publish_failure_alert", lambda rt, tid: raised.append((rt, tid))
    )
    return raised


@pytest.fixture
def db_engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ordering.db'}",
        connect_args={"check_same_thread": False, "timeout": 1},
    )
    _Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ci_ordering_widget (id, limit_value) VALUES ('w1', 100)"))
    # The real get_db, bound to the throwaway database.
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    yield engine
    engine.dispose()


@pytest.fixture
def events():
    """'commit' (root after_commit) and 'publish' in the order they happened."""
    seen = []

    def _spy(session):
        if not session.in_nested_transaction():
            seen.append("commit")

    event.listen(Session, "after_commit", _spy)
    yield seen
    event.remove(Session, "after_commit", _spy)


def _set_limit(db: Session, value: int) -> None:
    db.get(_Widget, "w1").limit_value = value


def _build_app(redis_client) -> FastAPI:
    app = FastAPI()

    @app.put("/widgets/w1")
    async def update(request: Request, value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        await invalidate_proxy_cache(request, "model", "t1", "w1", action="update")
        return {"ok": True}

    @app.put("/widgets/w1/sync")
    def update_sync(request: Request, value: int, db: Session = Depends(get_db)):
        # Sync handlers run in the threadpool and bridge to the loop.
        _set_limit(db, value)
        db.flush()
        block_on(invalidate_proxy_cache(request, "model", "t1", "w1", action="update"))
        return {"ok": True}

    @app.put("/widgets/w1/fails")
    async def update_then_fail(request: Request, value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        db.flush()
        await invalidate_proxy_cache(request, "model", "t1", "w1", action="update")
        raise HTTPException(status_code=409, detail="conflict after invalidating")

    @app.put("/widgets/w1/explicit")
    async def update_explicit(request: Request, value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        db.commit()
        db.get(_Widget, "w1")  # a read after commit autobegins a new, clean txn
        await invalidate_proxy_cache(request, "model", "t1", "w1", action="update")
        request.app.state.after_invalidate_publishes = len(redis_client.published)
        return {"ok": True}

    @app.put("/widgets/w1/repeat")
    async def update_repeat(request: Request, value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        for _ in range(3):
            await invalidate_proxy_cache(request, "model", "t1", "w1", action="update")
        await invalidate_proxy_cache(request, "guardrail", "t1", "g1", action="update")
        await invalidate_proxy_cache(request, "model", "t1", "w1", action="delete")
        return {"ok": True}

    @app.post("/widgets/duplicate")
    async def insert_duplicate(request: Request, db: Session = Depends(get_db)):
        # Unflushed: the primary-key clash only surfaces at commit.
        db.add(_Widget(id="w1", limit_value=1))
        await invalidate_proxy_cache(request, "model", "t1", "w1", action="create")
        return {"ok": True}

    # --- plain writes, no invalidation -----------------------------------

    @app.put("/plain/w1")
    async def plain_write(value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        return {"ok": True}

    @app.post("/plain/duplicate")
    def plain_duplicate(db: Session = Depends(get_db)):
        db.add(_Widget(id="w1", limit_value=1))  # clash surfaces at commit
        return {"ok": True}

    @app.get("/plain/w1")
    def plain_read(db: Session = Depends(get_db)):
        return {"limit": db.get(_Widget, "w1").limit_value}

    @app.put("/plain/w1/explicit")
    def plain_explicit(value: int, db: Session = Depends(get_db)):
        _set_limit(db, value)
        db.commit()
        return {"ok": True}

    @app.delete("/plain/w1", status_code=204)
    def plain_delete(db: Session = Depends(get_db)):
        db.delete(db.get(_Widget, "w1"))

    @app.put("/plain/w1/stream")
    def plain_stream(value: int, db: Session = Depends(get_db)):
        from fastapi.responses import StreamingResponse

        _set_limit(db, value)

        def body():
            yield b"a"
            yield b"b"

        return StreamingResponse(body(), media_type="text/plain")

    # Same shape as app/app.py: deferral middleware innermost, a
    # BaseHTTPMiddleware in between, the warning middleware outermost.
    app.add_middleware(CommitBeforeResponseMiddleware)
    app.add_middleware(_PassThrough)
    app.add_middleware(CacheInvalidationWarningMiddleware)
    app.state.redis = redis_client
    tapped_app = _AtResponseStart(app)
    _BUILT.append(tapped_app)
    return tapped_app


_BUILT: list = []


@pytest.fixture(autouse=True)
def _remove_tap_listeners():
    yield
    while _BUILT:
        _BUILT.pop().close()


class _AtResponseStart:
    """Outermost tap: what a separate connection sees of the row when the
    response starts (i.e. when the client could first act on it), and the
    order of root commits vs. that moment."""

    def __init__(self, app):
        self.app = app
        self.state = app.state
        self.row_at_start = None
        self.timeline = []

        def _spy(session):
            if not session.in_nested_transaction():
                self.timeline.append("commit")

        self._spy = _spy
        event.listen(Session, "after_commit", _spy)

    async def __call__(self, scope, receive, send):
        async def tap(message):
            if message["type"] == "http.response.start":
                engine = database.SessionLocal.kw["bind"]
                try:
                    self.row_at_start = _row(engine)
                except Exception:
                    self.row_at_start = "gone"
                self.timeline.append("response.start")
            await send(message)
        await self.app(scope, receive, tap)

    def close(self):
        event.remove(Session, "after_commit", self._spy)


def _row(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT limit_value FROM ci_ordering_widget WHERE id = 'w1'")
        ).scalar()


# ---------------------------------------------------------------------------
# Commit before the response: every request with writes, not only those
# that invalidate. get_db's own commit runs after the response is sent, so
# a commit failure there used to reach the client as a 200.
# ---------------------------------------------------------------------------

@pytest.fixture
def tapped(db_engine):
    return lambda redis_client=None: _build_app(redis_client or _RecordingRedis(db_engine, []))


def test_write_is_committed_before_the_response_starts(db_engine, tapped):
    app = tapped()
    resp = TestClient(app).put("/plain/w1", params={"value": 42})

    assert resp.status_code == 200
    assert app.row_at_start == 42, "client could see 200 before the write was durable"
    assert app.timeline.index("commit") < app.timeline.index("response.start")


def test_commit_failure_is_a_500_not_a_200(db_engine, tapped):
    app = tapped()
    resp = TestClient(app, raise_server_exceptions=False).post("/plain/duplicate")

    assert resp.status_code == 500, "a change that was never saved came back as success"
    assert _row(db_engine) == 100


def test_read_only_request_does_not_commit_before_the_response(db_engine, tapped):
    app = tapped()
    resp = TestClient(app).get("/plain/w1")

    assert resp.status_code == 200 and resp.json() == {"limit": 100}
    # get_db's backstop commit after the response is the only one.
    assert "commit" not in app.timeline[: app.timeline.index("response.start")]


def test_handler_that_commits_itself_gets_no_second_commit(db_engine, tapped):
    app = tapped()
    resp = TestClient(app).put("/plain/w1/explicit", params={"value": 8})

    assert resp.status_code == 200
    assert app.row_at_start == 8
    assert app.timeline[: app.timeline.index("response.start")] == ["commit"]


def test_204_delete_is_committed_before_the_response(db_engine, tapped):
    app = tapped()
    resp = TestClient(app).delete("/plain/w1")

    assert resp.status_code == 204
    assert app.row_at_start is None  # row gone by the time headers go out


def test_streaming_response_commits_before_the_first_byte(db_engine, tapped):
    app = tapped()
    resp = TestClient(app).put("/plain/w1/stream", params={"value": 13})

    assert resp.status_code == 200 and resp.text == "ab"
    assert app.row_at_start == 13


# ---------------------------------------------------------------------------
# (1) publish happens after the commit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/widgets/w1", "/widgets/w1/sync"])
def test_publish_happens_after_commit(db_engine, events, alerts, path):
    redis = _RecordingRedis(db_engine, events)
    client = TestClient(_build_app(redis))

    resp = client.put(path, params={"value": 5})

    assert resp.status_code == 200, resp.text
    assert len(redis.published) == 1
    _, message, seen = redis.published[0]
    assert message == {"type": "model", "action": "update", "tenant_id": "t1", "id": "w1"}
    # A proxy reloading on this message must see the NEW row.
    assert seen == 5, f"published while the change was uncommitted (row was {seen})"
    assert events.index("commit") < events.index("publish")
    assert "x-brutor-cache-invalidated" not in resp.headers
    assert alerts == []


# ---------------------------------------------------------------------------
# (2) rollback → nothing published
# ---------------------------------------------------------------------------

def test_rolled_back_change_is_not_published(db_engine, events, alerts):
    redis = _RecordingRedis(db_engine, events)
    client = TestClient(_build_app(redis))

    resp = client.put("/widgets/w1/fails", params={"value": 5})

    assert resp.status_code == 409
    assert _row(db_engine) == 100
    assert redis.published == [], "announced a change that was rolled back"
    assert alerts == []


# ---------------------------------------------------------------------------
# (3) handler committed explicitly → published immediately, as before
# ---------------------------------------------------------------------------

def test_explicit_commit_publishes_immediately(db_engine, events, alerts):
    redis = _RecordingRedis(db_engine, events)
    app = _build_app(redis)
    client = TestClient(app)

    resp = client.put("/widgets/w1/explicit", params={"value": 7})

    assert resp.status_code == 200
    assert app.state.after_invalidate_publishes == 1, "should not wait for teardown"
    assert len(redis.published) == 1
    assert redis.published[0][2] == 7


# ---------------------------------------------------------------------------
# (4) dedupe
# ---------------------------------------------------------------------------

def test_identical_deferred_invalidations_are_published_once(db_engine, events, alerts):
    redis = _RecordingRedis(db_engine, events)
    client = TestClient(_build_app(redis))

    resp = client.put("/widgets/w1/repeat", params={"value": 9})

    assert resp.status_code == 200
    assert [(m["type"], m["id"], m["action"]) for _, m, _ in redis.published] == [
        ("model", "w1", "update"),
        ("guardrail", "g1", "update"),
        ("model", "w1", "delete"),
    ]
    assert all(seen == 9 for _, _, seen in redis.published)


# ---------------------------------------------------------------------------
# (5) a failed publish after the commit still reaches the response + alert
# ---------------------------------------------------------------------------

def test_publish_failure_after_commit_still_warns(db_engine, alerts):
    client = TestClient(_build_app(_DeadRedis()))

    resp = client.put("/widgets/w1", params={"value": 11})

    assert resp.status_code == 200
    assert _row(db_engine) == 11
    assert resp.headers.get("x-brutor-cache-invalidated") == "false"
    assert "invalidation could not be published" in resp.headers.get("warning", "")
    assert alerts == [("model", "t1")]


# ---------------------------------------------------------------------------
# A commit that fails is a 500, not a 200 — and nothing is announced
# ---------------------------------------------------------------------------

def test_failed_commit_is_an_error_and_publishes_nothing(db_engine, events, alerts):
    redis = _RecordingRedis(db_engine, events)
    client = TestClient(_build_app(redis), raise_server_exceptions=False)

    resp = client.post("/widgets/duplicate")

    assert resp.status_code == 500
    assert redis.published == []
    assert _row(db_engine) == 100


# ---------------------------------------------------------------------------
# Pins and class guards
# ---------------------------------------------------------------------------

def test_fastapi_runs_yield_teardown_after_the_response_is_sent():
    """Why the deferral middleware commits at response start: `get_db`'s
    own commit runs only once the response is already on the wire, too
    late for a header. If FastAPI moves teardown back before the send,
    this fails and the middleware's early commit can be revisited."""
    order = []

    def dep():
        yield
        order.append("teardown")

    app = FastAPI()

    @app.get("/x")
    def x(_=Depends(dep)):
        return {}

    async def outer(scope, receive, send):
        async def tap(message):
            if message["type"] == "http.response.start":
                order.append("response.start")
            await send(message)
        await app(scope, receive, tap)

    TestClient(outer).get("/x")
    assert order == ["response.start", "teardown"]


def test_deferral_middleware_is_innermost_in_the_real_app():
    """The early commit must run on the router's own send path. With a
    BaseHTTPMiddleware in between, the inner app (and get_db's teardown)
    runs in another task concurrently with the outer send — committing
    there would race the teardown on the same session."""
    from tests.app_factory import create_app

    stack = [m.cls for m in create_app().user_middleware]  # outermost first
    assert stack[-1] is CommitBeforeResponseMiddleware
    assert stack[0] is CacheInvalidationWarningMiddleware


def test_get_db_binds_the_session_to_the_request():
    """Deferral is keyed off the session get_db hands the request; a
    session dependency that skipped the binding would silently publish
    early again."""
    import inspect

    params = inspect.signature(get_db).parameters
    assert "request" in params and params["request"].annotation in (Request, "Request")
    src = inspect.getsource(get_db)
    assert "bind_request_session" in src


def test_invalidate_proxy_cache_defers_before_it_publishes():
    """Structural rule: inside invalidate_proxy_cache the deferral check
    precedes the (only) publish, so no code path reaches the publisher
    while the request's session holds uncommitted writes."""
    import inspect

    fn = ast.parse(inspect.getsource(invalidate_proxy_cache).lstrip())
    calls = [
        (n.lineno, n.func.id)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    names = [name for _, name in sorted(calls)]
    assert "_defer_until_commit" in names and "_publish_and_surface" in names
    assert names.index("_defer_until_commit") < names.index("_publish_and_surface")
    assert "publish_invalidation" not in names
