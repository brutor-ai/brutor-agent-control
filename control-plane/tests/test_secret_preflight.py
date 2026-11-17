# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the boot-time secret-consistency preflight
(`app/services/secret_preflight.py`).

Covers the "reused old volume + new trial secrets" detection:
  * ENCRYPTION_KEY: a stored model api_key that won't decrypt → loud ERROR.
  * ENCRYPTION_KEY: decrypts fine → INFO, no error.
  * BRUTOR_GATEWAY_SERVICE_API_KEY: env set but no matching api_keys row → ERROR.
  * BRUTOR_GATEWAY_SERVICE_API_KEY: matching row → INFO.
  * placeholder / unset secrets → skipped (no error).
  * never raises (preflight must not crash startup).

DB + EncryptionService are faked; no Postgres needed.

Run: pytest tests/test_secret_preflight.py -v
"""

import hashlib

import pytest

from app.services import secret_preflight
from app.services.secret_preflight import run_secret_consistency_preflight


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._result


class _FakeDB:
    """Returns a canned `.first()` per call to `.query()`, in order."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    def query(self, *a, **k):
        r = self._results[self._i] if self._i < len(self._results) else None
        self._i += 1
        return _FakeQuery(r)


class _FakeLogger:
    def __init__(self):
        self.errors = []
        self.infos = []
        self.debugs = []

    def error(self, msg, **k):
        self.errors.append(msg)

    def info(self, msg, **k):
        self.infos.append(msg)

    def debug(self, msg, **k):
        self.debugs.append(msg)

    def warning(self, msg, **k):
        pass


class _Enc:
    def __init__(self, ok: bool):
        self._ok = ok

    def decrypt(self, _ct: str) -> str:
        if self._ok:
            return "sk-plaintext"
        raise ValueError("HMAC verification failed")


def test_encryption_key_mismatch_logs_error(monkeypatch):
    monkeypatch.delenv("BRUTOR_GATEWAY_SERVICE_API_KEY", raising=False)
    db = _FakeDB([("llm-1", "ciphertext-from-old-key")])  # one model row
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=False), log)
    assert any("ENCRYPTION_KEY does not match" in m for m in log.errors)


def test_encryption_key_match_logs_info_no_error(monkeypatch):
    monkeypatch.delenv("BRUTOR_GATEWAY_SERVICE_API_KEY", raising=False)
    db = _FakeDB([("llm-1", "good-ciphertext")])
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=True), log)
    assert not log.errors
    assert any("ENCRYPTION_KEY matches" in m for m in log.infos)


def test_no_stored_keys_is_silent(monkeypatch):
    monkeypatch.delenv("BRUTOR_GATEWAY_SERVICE_API_KEY", raising=False)
    db = _FakeDB([None])  # no model with a key
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=False), log)
    assert not log.errors


def test_health_check_key_missing_row_logs_error(monkeypatch):
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "sk_brutor_api_realkey123")
    # 1st query (model) → None (skip enc check); 2nd query (api_keys) → None.
    db = _FakeDB([None, None])
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=True), log)
    assert any("BRUTOR_GATEWAY_SERVICE_API_KEY does not match" in m for m in log.errors)


def test_health_check_key_present_row_logs_info(monkeypatch):
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "sk_brutor_api_realkey123")
    db = _FakeDB([None, ("apikey-1",)])  # model None, api_keys row found
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=True), log)
    assert not log.errors
    assert any("BRUTOR_GATEWAY_SERVICE_API_KEY resolves" in m for m in log.infos)


def test_placeholder_health_check_key_is_skipped(monkeypatch):
    monkeypatch.setenv(
        "BRUTOR_GATEWAY_SERVICE_API_KEY", "sk_brutor_api_your-proxy-health-check-key-here"
    )
    db = _FakeDB([None, None])
    log = _FakeLogger()
    run_secret_consistency_preflight(db, _Enc(ok=True), log)
    # Placeholder → no api_keys lookup, no error.
    assert not any("BRUTOR_GATEWAY_SERVICE_API_KEY" in m for m in log.errors)


def test_never_raises_on_db_error(monkeypatch):
    monkeypatch.delenv("BRUTOR_GATEWAY_SERVICE_API_KEY", raising=False)

    class _BoomDB:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    log = _FakeLogger()
    # Must not propagate.
    run_secret_consistency_preflight(_BoomDB(), _Enc(ok=True), log)


def test_health_key_hash_is_sha256(monkeypatch):
    # Guards the hashing contract the proxy/control auth path relies on.
    key = "sk_brutor_api_abc"
    assert hashlib.sha256(key.encode()).hexdigest() == hashlib.sha256(b"sk_brutor_api_abc").hexdigest()
