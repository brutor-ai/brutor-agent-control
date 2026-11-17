# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests: the cache-invalidation publisher must survive a Redis
that is unreachable when the control plane boots.

The 0.11.25 trial stack started the control plane before Redis answered.
The lifespan pinged Redis, the ping timed out, and `app.state.redis` was
left as None for the life of the process — every admin config change and
"Clear cache" silently fell back to the proxy's TTL refresh until restart.

The bug class guarded here is "unavailable at boot -> disabled forever":

  * the publisher is created lazily and kept even when the boot probe
    fails, so a later publish reconnects once Redis is reachable;
  * a failed publish never raises into the admin request;
  * an outage is logged once (WARNING) and its end once (INFO), not per
    publish.

Mocks only; no Redis, no database.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import cache_invalidation as ci

LOGGER = "cache-invalidation"


class FakeRedis:
    """Stands in for `redis.asyncio.Redis`: every command fails with a
    connection error until `reachable` is flipped, then succeeds — the
    same observable behaviour as redis-py's pool reconnecting per call."""

    def __init__(self):
        self.reachable = False
        self.published = []

    def _check(self):
        if not self.reachable:
            raise ConnectionError("Timeout connecting to server")

    async def ping(self):
        self._check()
        return True

    async def publish(self, channel, message):
        self._check()
        self.published.append((channel, message))
        return 1

    async def aclose(self):
        return None


@pytest.fixture(autouse=True)
def _reset_publisher_health():
    health = getattr(ci, "_publisher_health", None)
    if health is not None:
        health.reset()
    yield
    if health is not None:
        health.reset()


@pytest.fixture
def fake_from_url(monkeypatch):
    """Route `redis.asyncio.from_url` to a FakeRedis and record kwargs."""
    import redis.asyncio as redis_async

    calls = {}
    fake = FakeRedis()

    def _from_url(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(redis_async, "from_url", _from_url)
    return fake, calls


@pytest.fixture
def no_alert(monkeypatch):
    """The SystemAlert path opens a DB session — keep it out of unit tests."""
    raised = []
    monkeypatch.setattr(
        ci, "_raise_publish_failure_alert", lambda rt, tid: raised.append((rt, tid))
    )
    return raised


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def _infos(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


# ---------------------------------------------------------------------------
# Startup failure must not disable the publisher for the process lifetime
# ---------------------------------------------------------------------------

class TestStartupOutageRecovers:

    @pytest.mark.asyncio
    async def test_publisher_kept_when_boot_probe_fails_and_publishes_after_recovery(
        self, fake_from_url
    ):
        fake, _ = fake_from_url

        client = ci.create_invalidation_publisher("redis://redis:6379/0")
        assert client is fake, "client must be created without a live connection"

        error = await ci.probe_invalidation_publisher(client)
        assert error is not None and "Timeout" in error

        # Redis comes up after boot — the same client must now publish.
        fake.reachable = True
        ok = await ci.publish_invalidation(client, "resource_group", "default", "rg-1", "update")
        assert ok is True
        assert len(fake.published) == 1
        assert fake.published[0][0] == ci.INVALIDATION_CHANNEL

    @pytest.mark.asyncio
    async def test_probe_returns_none_when_reachable(self, fake_from_url):
        fake, _ = fake_from_url
        fake.reachable = True
        client = ci.create_invalidation_publisher("redis://redis:6379/0")
        assert await ci.probe_invalidation_publisher(client) is None

    def test_client_uses_bounded_timeouts_and_retries(self, fake_from_url):
        """Reconnect happens on the request path, so each attempt must be
        bounded: short connect/op timeouts and at most one retry (the one
        retry replaces a stale pooled socket after a Redis restart)."""
        _, calls = fake_from_url
        ci.create_invalidation_publisher("redis://redis:6379/0")
        kw = calls["kwargs"]
        assert 0 < kw["socket_connect_timeout"] <= 2.0
        assert 0 < kw["socket_timeout"] <= 2.0
        retry = kw["retry"]
        assert retry is not None
        assert retry._retries <= 1

    def test_lifespan_keeps_client_even_when_probe_fails(self):
        """Wiring guard: the startup step must assign `app.state.redis` from
        the lazy factory, not only after a successful ping. (The step moved
        from the lifespan in app/app.py to app/composition/startup.py;
        tests/test_startup_registry.py checks the same thing by running it.)"""
        src = (Path(__file__).resolve().parents[1] / "app" / "composition" / "startup.py").read_text()
        assert "app.state.redis = create_invalidation_publisher(" in src
        assert "await probe_invalidation_publisher(app.state.redis)" in src
        # The old shape: ping, and only then publish the client on app.state.
        assert "await _redis_client.ping()" not in src


# ---------------------------------------------------------------------------
# A failed publish never fails the admin request
# ---------------------------------------------------------------------------

class TestFailedPublishDoesNotRaise:

    @pytest.mark.asyncio
    async def test_invalidate_proxy_cache_returns_false_and_records(self, no_alert):
        fake = FakeRedis()  # unreachable
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=fake)), scope={})

        ok = await ci.invalidate_proxy_cache(request, "model", "default", "llm-1")

        assert ok is False
        assert request.scope["state"][ci.FAILURE_STATE_KEY] == [
            {"type": "model", "action": "update", "tenant_id": "default", "id": "llm-1"}
        ]
        assert no_alert == [("model", "default")]

    @pytest.mark.asyncio
    async def test_publish_swallows_unexpected_errors(self):
        class Boom(FakeRedis):
            async def publish(self, channel, message):
                raise RuntimeError("unexpected")

        assert await ci.publish_invalidation(Boom(), "model", "t", "x") is False


# ---------------------------------------------------------------------------
# Logging: once per outage, not per publish
# ---------------------------------------------------------------------------

class TestOutageLogging:

    @pytest.mark.asyncio
    async def test_warning_once_per_outage_and_info_on_recovery(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        fake = FakeRedis()

        for i in range(5):
            assert await ci.publish_invalidation(fake, "model", "t", f"x{i}") is False
        assert len(_warnings(caplog)) == 1

        fake.reachable = True
        for i in range(3):
            assert await ci.publish_invalidation(fake, "model", "t", f"y{i}") is True
        recovered = [r for r in _infos(caplog) if "recovered" in r.getMessage().lower()]
        assert len(recovered) == 1

        # A second, separate outage warns again — once.
        fake.reachable = False
        for i in range(4):
            await ci.publish_invalidation(fake, "model", "t", f"z{i}")
        assert len(_warnings(caplog)) == 2

    @pytest.mark.asyncio
    async def test_boot_probe_failure_counts_as_the_outage_warning(self, caplog, fake_from_url):
        """The lifespan logs the boot failure itself; subsequent publish
        failures in the same outage must not repeat it."""
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        fake, _ = fake_from_url
        client = ci.create_invalidation_publisher("redis://redis:6379/0")
        assert await ci.probe_invalidation_publisher(client) is not None

        for i in range(3):
            await ci.publish_invalidation(client, "model", "t", f"x{i}")
        assert _warnings(caplog) == []

        fake.reachable = True
        await ci.publish_invalidation(client, "model", "t", "ok")
        assert any("recovered" in r.getMessage().lower() for r in _infos(caplog))

    def test_sync_publisher_shares_outage_state(self, caplog):
        caplog.set_level(logging.DEBUG, logger=LOGGER)

        class SyncFake:
            reachable = False

            def publish(self, channel, message):
                if not self.reachable:
                    raise ConnectionError("down")
                return 1

        fake = SyncFake()
        for _ in range(3):
            assert ci.publish_invalidation_sync(fake, "model", "t", "x") is False
        assert len(_warnings(caplog)) == 1
        fake.reachable = True
        assert ci.publish_invalidation_sync(fake, "model", "t", "x") is True
        assert any("recovered" in r.getMessage().lower() for r in _infos(caplog))
