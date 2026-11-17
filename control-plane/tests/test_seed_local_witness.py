# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The deployment's own witness seed: env-driven, create-once,
same-operator, and never trusting a key that is not a 64-hex Ed25519 key.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database
from app.database.models.witness import Witness
from app.database import seed_local_witness as seed

PIN = "ab" * 32
URL = "http://brutor-witness:8000"


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Witness.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    for name in (seed.ENV_URL, seed.ENV_PINNED_KEY, seed.ENV_OPERATOR):
        monkeypatch.delenv(name, raising=False)
    yield factory
    engine.dispose()


def _rows(factory):
    db = factory()
    try:
        return db.query(Witness).all()
    finally:
        db.close()


def test_inert_without_url(session_factory):
    assert seed.seed_local_witness() is None
    assert _rows(session_factory) == []


def test_explicit_pin_seeds_same_operator_platform_witness(session_factory, monkeypatch):
    monkeypatch.setenv(seed.ENV_URL, URL + "/")
    monkeypatch.setenv(seed.ENV_PINNED_KEY, PIN.upper())
    seed.seed_local_witness()
    (row,) = _rows(session_factory)
    assert row.id == seed.LOCAL_WITNESS_ID
    assert (row.scope, row.tenant_id, row.kind) == ("platform", None, "scitt")
    assert row.url == URL
    assert row.pinned_key == PIN
    assert row.same_operator is True and row.enabled is True
    assert row.created_by == "system"


def test_create_once_never_overwrites_operator_edits(session_factory, monkeypatch):
    monkeypatch.setenv(seed.ENV_URL, URL)
    monkeypatch.setenv(seed.ENV_PINNED_KEY, PIN)
    seed.seed_local_witness()
    db = session_factory()
    row = db.get(Witness, seed.LOCAL_WITNESS_ID)
    row.operator_label = "edited"
    row.enabled = False
    db.commit()
    db.close()

    monkeypatch.setenv(seed.ENV_PINNED_KEY, "cd" * 32)
    seed.seed_local_witness()
    (row,) = _rows(session_factory)
    assert (row.operator_label, row.enabled, row.pinned_key) == ("edited", False, PIN)


@pytest.mark.parametrize("bad", ["abc", "zz" * 32, "ab" * 33])
def test_malformed_pin_is_refused(session_factory, monkeypatch, bad):
    """Refused by name where the variable is read (and at start), where it
    used to be logged and the witness quietly left out."""
    from app.env_registry import EnvError

    monkeypatch.setenv(seed.ENV_URL, URL)
    monkeypatch.setenv(seed.ENV_PINNED_KEY, bad)
    with pytest.raises(EnvError, match=f"{seed.ENV_PINNED_KEY} is not valid"):
        seed.seed_local_witness()
    assert _rows(session_factory) == []


def test_non_http_url_is_refused(session_factory, monkeypatch):
    from app.env_registry import EnvError

    monkeypatch.setenv(seed.ENV_URL, "file:///etc/passwd")
    monkeypatch.setenv(seed.ENV_PINNED_KEY, PIN)
    with pytest.raises(EnvError, match=f"{seed.ENV_URL} is not valid"):
        seed.seed_local_witness()
    assert _rows(session_factory) == []


def test_pin_fetched_from_witness_after_it_comes_up(session_factory, monkeypatch):
    monkeypatch.setenv(seed.ENV_URL, URL)
    answers = iter([None, None, PIN])
    calls = []

    def fake_fetch(url, timeout=5.0):
        calls.append(url)
        return next(answers)

    monkeypatch.setattr(seed, "fetch_pinned_key", fake_fetch)
    seed.seed_local_witness(background=False, deadline_s=5, interval_s=0)
    assert calls == [URL, URL, URL]
    (row,) = _rows(session_factory)
    assert row.pinned_key == PIN


def test_background_thread_seeds(session_factory, monkeypatch):
    monkeypatch.setenv(seed.ENV_URL, URL)
    monkeypatch.setattr(seed, "fetch_pinned_key", lambda url, timeout=5.0: PIN)
    thread = seed.seed_local_witness(deadline_s=5, interval_s=0)
    assert thread is not None
    thread.join(timeout=5)
    assert [r.id for r in _rows(session_factory)] == [seed.LOCAL_WITNESS_ID]


def test_unreachable_witness_gives_up_without_a_row(session_factory, monkeypatch):
    monkeypatch.setenv(seed.ENV_URL, URL)
    monkeypatch.setattr(seed, "fetch_pinned_key", lambda url, timeout=5.0: None)
    seed.seed_local_witness(background=False, deadline_s=0, interval_s=0)
    assert _rows(session_factory) == []


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"pubkey_hex": PIN.upper(), "key_id": "x"}, PIN),
        (200, {"pubkey_hex": "short"}, None),
        (200, ["not", "a", "dict"], None),
        (503, {"pubkey_hex": PIN}, None),
    ],
)
def test_fetch_pinned_key_parses_authority_pubkey(monkeypatch, status, body, expected):
    import httpx

    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return _Resp(status, body)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert seed.fetch_pinned_key(URL) == expected
    assert seen["url"] == URL + "/anchor/authority-pubkey"
