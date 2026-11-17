# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The acting admin has to survive the step into the threadpool

`get_current_admin` binds the actor; the flush listener reads it and stamps
`created_by` / `updated_by`. Between them FastAPI puts a threadpool.

`get_current_admin` is a **sync** function, so FastAPI runs it through
`run_in_threadpool`, which hands it a *copy* of the context. A
`ContextVar.set` inside a copy dies with the copy — invisible to the
parent, invisible to the endpoint, invisible to the flush. So the binding
wrote to a throwaway context on every request and the listener never saw an
actor at all.

The symptom was quiet and easy to misread: `updated_at` moved (endpoints
set it by hand) while `updated_by` stayed on whatever seeding had written.
It read as "the stamp is wrong" rather than "the stamp never ran". The only
resources that recorded an editor were the few whose endpoints assign
`updated_by` themselves — agent skills did, which is why skills alone
looked correct and made the failure look resource-specific.

The fix is a mutable slot installed per request, before any threadpool step.
Context copies share the slot *object*, so mutating it is visible
everywhere, while the slot is still per-request.

These tests drive a real FastAPI app through a real sync dependency,
because that boundary is the entire subject — a unit test that calls
`set_current_actor` and `get_current_actor` in one context passes happily
against the broken version.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.database.models.resource_mixins import (
    SYSTEM_ACTOR,
    acting_as,
    get_current_actor,
    open_actor_scope,
    set_current_actor,
)
from app.middleware.authorship import AuthorshipContextMiddleware


def _app() -> FastAPI:
    """A miniature of the real wiring: middleware, sync dep, sync endpoint."""
    app = FastAPI()
    app.add_middleware(AuthorshipContextMiddleware)

    def fake_admin_dependency(who: str = "alice") -> str:
        # Sync, exactly like `get_current_admin` — this is the threadpool
        # boundary the actor has to cross.
        set_current_actor(who)
        return who

    @app.get("/sync")
    def sync_endpoint(_admin: str = Depends(fake_admin_dependency)):
        # A sync endpoint is another threadpool step, and the flush happens
        # here.
        return {"actor": get_current_actor()}

    @app.get("/async")
    async def async_endpoint(_admin: str = Depends(fake_admin_dependency)):
        return {"actor": get_current_actor()}

    return app


@pytest.fixture(autouse=True)
def _clean_actor_context():
    """Reset both actor vars around every test.

    A real request gets a fresh context from the ASGI server, so the slot
    cannot leak between requests. Tests all share one context, so without
    this a test that opens a scope silently arms the next one.
    """
    from app.database.models import resource_mixins as rm

    slot_token = rm._actor_slot.set(None)
    var_token = rm._current_actor.set(None)
    try:
        yield
    finally:
        rm._actor_slot.reset(slot_token)
        rm._current_actor.reset(var_token)


@pytest.fixture()
def client():
    with TestClient(_app()) as c:
        yield c


class TestTheActorReachesTheFlush:
    def test_sync_endpoint_sees_the_admin(self, client):
        assert client.get("/sync").json()["actor"] == "alice"

    def test_async_endpoint_sees_the_admin(self, client):
        assert client.get("/async").json()["actor"] == "alice"

    def test_each_request_gets_its_own_actor(self, client):
        assert client.get("/sync?who=alice").json()["actor"] == "alice"
        assert client.get("/sync?who=bob").json()["actor"] == "bob"

    def test_an_actor_does_not_leak_into_the_next_request(self, client):
        assert client.get("/sync?who=alice").json()["actor"] == "alice"
        # No admin bound on this one — the previous operator must not be
        # credited with it.
        app = FastAPI()
        app.add_middleware(AuthorshipContextMiddleware)

        @app.get("/anon")
        def anon():
            return {"actor": get_current_actor()}

        with TestClient(app) as anon_client:
            assert anon_client.get("/anon").json()["actor"] is None


class TestScopePrecedence:
    def test_acting_as_beats_the_request_actor(self):
        # Tenant provisioning runs inside an admin request and binds
        # SYSTEM_ACTOR. The starter servers and skills it creates are
        # seeded content, not something that admin authored.
        open_actor_scope()
        set_current_actor("alice")
        assert get_current_actor() == "alice"

        with acting_as(SYSTEM_ACTOR):
            assert get_current_actor() == SYSTEM_ACTOR

        assert get_current_actor() == "alice", "the scope must not swallow the admin"

    def test_it_works_with_no_request_at_all(self):
        # Seeds at boot, CLI scripts, background jobs: no slot is open and
        # `acting_as` still has to work.
        with acting_as(SYSTEM_ACTOR):
            assert get_current_actor() == SYSTEM_ACTOR
        assert get_current_actor() is None

    def test_binding_without_a_slot_still_records_something(self):
        # A caller outside a request should not silently lose the actor.
        token = set_current_actor("carol")
        try:
            assert get_current_actor() == "carol"
        finally:
            from app.database.models.resource_mixins import reset_current_actor

            reset_current_actor(token)
        assert get_current_actor() is None


class TestSeedsAreAttributedToThePlatform:
    def test_no_seed_invents_its_own_actor_name(self):
        """Seeds must write `system`, not a per-seed variant.

        Two seeds used `system-seed` and `system-shared-seed`. An operator
        filtering on who created a resource then has to know all three
        spellings, and any they miss looks like a resource nobody created.
        """
        import pathlib

        from tests.source_trees import APP, python_sources

        seeds = [p for p in python_sources(APP / "database") if p.name.startswith("seed_")]
        assert len(seeds) > 5
        offenders = []
        for path in seeds:
            text = path.read_text()
            for variant in ('"system-seed"', '"system-shared-seed"'):
                if f"created_by={variant}" in text:
                    offenders.append(f"{path.name}: {variant}")
        assert offenders == [], (
            f"seeds should bind SYSTEM_ACTOR: {offenders}"
        )
