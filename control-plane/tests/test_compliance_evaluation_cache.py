# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""The compliance evaluation is computed once per live window, by one request.

The bug class: a cache whose key changes faster than its value is useful. The
console asks for "the last 30 days" with an end rounded to the minute, the
key was that window, so every minute was a cold evaluation (6 to 19 s on the
demo's ledger), and each concurrent request on a cold key computed it again.

Guards:
  * a live window is snapped to the grid: every request of one grid step
    evaluates the same window, of the length asked for;
  * a past window is evaluated exactly as asked;
  * concurrent requests for one key compute it once (single flight);
  * a value computed before `clear_cache` is not stored after it.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.services.compliance import evaluation
from app.services.compliance.evaluation import LIVE_WINDOW_GRID_SECONDS, parse_window

NOW = datetime(2026, 10, 1, 7, 13, 42, tzinfo=timezone.utc)
GRID = timedelta(seconds=LIVE_WINDOW_GRID_SECONDS)


@pytest.fixture(autouse=True)
def _empty_cache():
    evaluation.clear_cache()
    yield
    evaluation.clear_cache()


class TestLiveWindowGrid:
    def test_the_default_window_ends_on_the_grid(self):
        start, end = parse_window(None, None, now=NOW)
        assert end == datetime(2026, 10, 1, 7, 10, tzinfo=timezone.utc)
        assert end - start == timedelta(days=evaluation.DEFAULT_WINDOW_DAYS)

    def test_every_request_of_one_grid_step_evaluates_the_same_window(self):
        """What the console sends: `to` = now rounded down to the minute,
        `from` = `to` minus the preset, re-computed on every load."""
        windows = set()
        for seconds in range(0, LIVE_WINDOW_GRID_SECONDS, 7):
            now = datetime(2026, 10, 1, 7, 10, tzinfo=timezone.utc) + timedelta(seconds=seconds)
            to = now.replace(second=0, microsecond=0)
            windows.add(parse_window(to - timedelta(days=30), to, now=now))
        assert len(windows) == 1
        (start, end), = windows
        assert end - start == timedelta(days=30)

    def test_the_next_grid_step_is_a_new_window(self):
        a = parse_window(None, None, now=NOW)
        b = parse_window(None, None, now=NOW + GRID)
        assert b[1] - a[1] == GRID

    def test_an_end_rounded_up_past_now_is_live_too(self):
        _, end = parse_window(None, NOW + timedelta(seconds=30), now=NOW)
        assert end == datetime(2026, 10, 1, 7, 10, tzinfo=timezone.utc)

    def test_a_past_window_is_evaluated_exactly_as_asked(self):
        start = datetime(2026, 8, 1, 0, 0, 7, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, 0, 0, 7, tzinfo=timezone.utc)
        assert parse_window(start, end, now=NOW) == (start, end)

    def test_the_window_never_reaches_past_now(self):
        for seconds in range(0, 2 * LIVE_WINDOW_GRID_SECONDS, 11):
            now = NOW + timedelta(seconds=seconds)
            assert parse_window(None, None, now=now)[1] <= now


class TestSingleFlight:
    def test_concurrent_requests_compute_once(self):
        calls = []

        def slow():
            calls.append(threading.current_thread().name)
            time.sleep(0.3)
            return {"value": len(calls)}

        results = []
        threads = [threading.Thread(target=lambda: results.append(evaluation.cached(("k", "t1"), slow)))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1, f"computed {len(calls)} times for one key"
        assert results == [{"value": 1}] * 6

    def test_different_keys_do_not_wait_for_each_other(self):
        def slow():
            time.sleep(0.3)
            return 1

        started = time.perf_counter()
        threads = [threading.Thread(target=evaluation.cached, args=(("k", "t1", i), slow)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert time.perf_counter() - started < 0.3 * 4 * 0.6

    def test_a_failed_computation_is_retried_not_cached(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("core unreachable")
            return "ok"

        with pytest.raises(RuntimeError):
            evaluation.cached(("k", "t1"), flaky)
        assert evaluation.cached(("k", "t1"), flaky) == "ok"

    def test_a_value_is_kept_for_the_grid_step(self):
        assert evaluation.CACHE_TTL_SECONDS == LIVE_WINDOW_GRID_SECONDS
        calls = []
        for _ in range(3):
            evaluation.cached(("k", "t1"), lambda: calls.append(1))
        assert len(calls) == 1

    def test_a_value_computed_before_a_clear_is_not_stored(self):
        """A declaration saved while an evaluation runs: the running one saw
        the old profile, so its result must not outlive the clear."""
        calls = []

        def compute():
            calls.append(1)
            if len(calls) == 1:
                evaluation.clear_cache("t1")  # the profile was saved meanwhile
            return len(calls)

        assert evaluation.cached(("k", "t1"), compute) == 1
        assert evaluation.cached(("k", "t1"), compute) == 2
