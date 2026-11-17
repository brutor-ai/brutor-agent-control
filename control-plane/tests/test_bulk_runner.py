# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for :class:`BulkRunner`.

Pin two invariants:

1. **Concurrency cap is honoured under load** — at no point are more
   than `cap` coroutines simultaneously in flight, regardless of how
   many inputs you give it. Catches the original bug where every
   admin endpoint owned its own ``Semaphore(N)`` with N hardcoded
   inconsistently.

2. **Per-item errors do not halt the batch** — a misbehaving model
   shouldn't sink the other 69 in a 70-model bulk health check.
"""

import asyncio
import os
from typing import List
from unittest.mock import MagicMock

import pytest

from app.services.concurrency import (
    BulkRunner,
    derive_concurrency_cap,
    DEFAULT_POOL_RESERVE,
)


# ───────────────────────────────────────────────────────────────────
# derive_concurrency_cap
# ───────────────────────────────────────────────────────────────────


class TestDeriveConcurrencyCap:
    def test_default_pool_yields_known_value(self):
        # BRUTOR_GATEWAY_DB_POOL_SIZE=20 * (1 - 0.4) = 12. Pin so future
        # tweaks to either default surface in this test rather than
        # silently changing every endpoint's effective cap.
        cap = derive_concurrency_cap(proxy_pool_size=20, pool_reserve=0.4)
        assert cap == 12

    def test_low_floor_for_tiny_pool(self):
        # A misconfigured tiny pool must still allow some concurrency,
        # not lock to 1.
        cap = derive_concurrency_cap(proxy_pool_size=2, pool_reserve=0.4)
        assert cap == 2  # floor

    def test_high_ceiling_for_huge_pool(self):
        # A 500-conn pool with default reserve would derive 300; we
        # cap to 64 because beyond that the proxy is bottlenecked on
        # something OTHER than connection count (CPU, upstream RPS).
        cap = derive_concurrency_cap(proxy_pool_size=500, pool_reserve=0.4)
        assert cap == 64  # ceiling

    def test_invalid_negative_reserve_clamps_to_zero(self):
        cap = derive_concurrency_cap(proxy_pool_size=100, pool_reserve=-0.5)
        # Reserve=0 → use full pool → clamped at 64 ceiling.
        assert cap == 64

    def test_invalid_high_reserve_clamps_to_95pct(self):
        cap = derive_concurrency_cap(proxy_pool_size=100, pool_reserve=0.99)
        # 100 * (1 - 0.95) = 5
        assert cap == 5

    def test_the_gateways_pool_size_is_read_and_the_reserve_is_fixed(self, monkeypatch):
        monkeypatch.setenv("BRUTOR_GATEWAY_DB_POOL_SIZE", "60")
        cap = derive_concurrency_cap()  # read from env
        assert cap == 36  # 60 * (1 - 0.4)

    @pytest.mark.parametrize("value", ["not-a-number", ""])
    def test_a_pool_size_that_is_no_number_is_refused(self, monkeypatch, value):
        """It used to become the default, 20, without a word: a bulk
        operation then ran against a pool size nobody had configured."""
        from app.env_registry import EnvError

        monkeypatch.setenv("BRUTOR_GATEWAY_DB_POOL_SIZE", value)
        with pytest.raises(EnvError, match="BRUTOR_GATEWAY_DB_POOL_SIZE is not valid"):
            derive_concurrency_cap()

    def test_unset_is_the_default(self, monkeypatch):
        monkeypatch.delenv("BRUTOR_GATEWAY_DB_POOL_SIZE", raising=False)
        assert derive_concurrency_cap() == 12


# ───────────────────────────────────────────────────────────────────
# BulkRunner
# ───────────────────────────────────────────────────────────────────


class TestBulkRunner:
    @pytest.mark.asyncio
    async def test_runs_all_inputs_in_order(self):
        async def double(x: int) -> int:
            return x * 2

        result = await BulkRunner(
            coroutine_factory=double,
            inputs=[1, 2, 3, 4, 5],
            op_kind="test",
            logger=MagicMock(),
            max_concurrency_override=2,
        ).run()
        assert result.results == [2, 4, 6, 8, 10]
        assert result.errors == [None] * 5
        assert result.success_count == 5
        assert result.error_count == 0
        assert result.total == 5

    @pytest.mark.asyncio
    async def test_concurrency_cap_is_enforced(self):
        # Track peak concurrency by atomic counter. With cap=4 and
        # 20 inputs that each sleep for a measurable interval, peak
        # MUST equal 4 (we never see more than 4 in-flight).
        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def slow(_x: int) -> int:
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                if in_flight > peak:
                    peak = in_flight
            try:
                # Sleep long enough that the asyncio scheduler
                # actually parks tasks, not just one tick.
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    in_flight -= 1
            return 0

        await BulkRunner(
            coroutine_factory=slow,
            inputs=list(range(20)),
            op_kind="cap_test",
            logger=MagicMock(),
            max_concurrency_override=4,
        ).run()
        assert peak == 4, f"peak={peak} exceeded cap=4"

    @pytest.mark.asyncio
    async def test_per_item_error_does_not_halt_batch(self):
        async def maybe_fail(x: int) -> int:
            if x == 3:
                raise RuntimeError("item 3 always fails")
            return x

        result = await BulkRunner(
            coroutine_factory=maybe_fail,
            inputs=[1, 2, 3, 4, 5],
            op_kind="error_test",
            logger=MagicMock(),
            max_concurrency_override=3,
        ).run()
        assert result.success_count == 4
        assert result.error_count == 1
        assert result.results[2] is None
        assert isinstance(result.errors[2], RuntimeError)
        # Other items completed normally.
        assert result.results == [1, 2, None, 4, 5]
        assert all(result.errors[i] is None for i in (0, 1, 3, 4))

    @pytest.mark.asyncio
    async def test_override_above_derived_is_clamped(self):
        # Operator passes a high override but the derived cap is
        # lower. The runner must use the lower value to honour the
        # whole "shared primitive" promise. Going above the derived
        # cap is the bug we built this to prevent.
        # Derived for default env should be 12.
        runner = BulkRunner(
            coroutine_factory=lambda x: asyncio.sleep(0, result=x),
            inputs=[1, 2, 3],
            op_kind="clamp_test",
            logger=MagicMock(),
            max_concurrency_override=999,
        )
        assert runner.concurrency == 12  # clamped to derived

    @pytest.mark.asyncio
    async def test_override_below_derived_is_honoured(self):
        # Operator says "this op holds 3 connections per unit", so
        # cap below the derived ceiling. Honour it.
        runner = BulkRunner(
            coroutine_factory=lambda x: asyncio.sleep(0, result=x),
            inputs=[1, 2, 3],
            op_kind="tighten_test",
            logger=MagicMock(),
            max_concurrency_override=2,
        )
        assert runner.concurrency == 2

    @pytest.mark.asyncio
    async def test_empty_inputs_returns_empty_result(self):
        result = await BulkRunner(
            coroutine_factory=lambda x: asyncio.sleep(0, result=x),
            inputs=[],
            op_kind="empty_test",
            logger=MagicMock(),
        ).run()
        assert result.total == 0
        assert result.success_count == 0
        assert result.error_count == 0
        assert result.results == []
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_completion_log_emitted_with_expected_fields(self):
        logger = MagicMock()
        await BulkRunner(
            coroutine_factory=lambda x: asyncio.sleep(0, result=x),
            inputs=[1, 2, 3],
            op_kind="my_op",
            logger=logger,
            max_concurrency_override=2,
        ).run()
        # The runner emits exactly one info() call: the
        # bulk_op_completed structured log. Find it (logger may have
        # other uses in real code but in tests with a fresh mock
        # it's the only call).
        info_calls = logger.info.call_args_list
        assert any("bulk_op_completed" in str(c) for c in info_calls)
        # And the context carries the labels operators rely on.
        ctx = next(
            c.kwargs["context"] for c in info_calls if "bulk_op_completed" in str(c)
        )
        assert ctx["op_kind"] == "my_op"
        assert ctx["total_items"] == 3
        assert ctx["concurrency"] == 2
        assert ctx["success_count"] == 3
        assert ctx["error_count"] == 0
        assert "duration_ms" in ctx
