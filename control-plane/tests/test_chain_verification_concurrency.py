# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Chain verification never runs one core round trip after another.

The bug class: a resolver that makes N slow remote calls in sequence. The
compliance evaluation verified up to ten audit chains per system, each a core
request that hashes thousands of rows (about a second each on the demo), one
after another and again for every system in the tenant. Compliance → Overview
took longer than the console's 30 s timeout.

Guards:
  * ranges are verified concurrently (wall time is bounded by the slowest
    call, not the sum);
  * a range is verified once per evaluation, however many systems share it;
  * the per-system report keeps its order and the not-checked cap.
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.services.compliance.resolvers import records
from app.services.compliance.resolvers._base import ResolverContext


class _Ctx:
    """The two things `verify_ranges` reads from a ResolverContext."""

    def __init__(self, shared=None):
        self.tenant_id = "t1"
        self.shared = shared if shared is not None else {}


def _slow_verifier(delay, log):
    def fake(chain_id, **kw):
        log.append((chain_id, kw["from_seq"], kw["limit"], threading.current_thread().name))
        time.sleep(delay)
        return {"verdict": "verified", "truncated": False, "undeclared_gaps": [], "pruned_rows": 0}
    return fake


def test_ranges_are_verified_concurrently(monkeypatch):
    log = []
    monkeypatch.setattr(records.core_client, "verify_chain", _slow_verifier(0.3, log))
    ranges = [(f"chain-{i}", 0, 99) for i in range(5)]
    started = time.perf_counter()
    out = records.verify_ranges(_Ctx(), ranges)
    elapsed = time.perf_counter() - started
    assert len(log) == 5 and all(v["verdict"] == "verified" for v in out.values())
    assert elapsed < 0.3 * 5 * 0.6, f"five 0.3 s verifications took {elapsed:.2f} s: sequential"
    assert len({t for *_, t in log}) > 1, "all verifications ran on one thread"


def test_a_range_is_verified_once_per_evaluation(monkeypatch):
    log = []
    monkeypatch.setattr(records.core_client, "verify_chain", _slow_verifier(0, log))
    shared = {}
    # Two systems of one evaluation whose rows share chain-a over the same range.
    first = records.verify_ranges(_Ctx(shared), [("chain-a", 0, 9), ("chain-b", 0, 9)])
    second = records.verify_ranges(_Ctx(shared), [("chain-a", 0, 9), ("chain-c", 5, 9)])
    assert [c for c, *_ in log] == ["chain-a", "chain-b", "chain-c"]
    assert first[("chain-a", 0, 9)] is second[("chain-a", 0, 9)]
    # A different range of the same chain is a different verification.
    records.verify_ranges(_Ctx(shared), [("chain-a", 0, 19)])
    assert [c for c, *_ in log] == ["chain-a", "chain-b", "chain-c", "chain-a"]


def test_the_core_is_asked_for_exactly_the_range(monkeypatch):
    log = []
    monkeypatch.setattr(records.core_client, "verify_chain", _slow_verifier(0, log))
    records.verify_ranges(_Ctx(), [("chain-a", 10, 12)])
    assert log[0][:3] == ("chain-a", 10, 3)


def test_an_error_is_never_a_verdict(monkeypatch):
    monkeypatch.setattr(records.core_client, "verify_chain",
                        lambda chain_id, **kw: {"error": "core_unreachable", "detail": "refused"})
    out = records.verify_ranges(_Ctx(), [("chain-a", 0, 9)])
    assert out[("chain-a", 0, 9)] == {"verdict": None, "error": "core_unreachable", "detail": "refused"}


def test_resolver_context_carries_the_shared_memo():
    """`verify_ranges` relies on `ResolverContext.shared` outliving one system."""
    assert "shared" in ResolverContext.__dataclass_fields__
