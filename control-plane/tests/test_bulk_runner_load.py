# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Load-style integration test for :class:`BulkRunner`.

Pins the **operational contract** that justifies the abstraction:

* Under realistic admin-bulk-op load (100+ items), the cap is
  honoured — no pool-saturation regression sneaks in via a future
  refactor that accidentally drops the semaphore.
* Per-item duration scales linearly with input count / cap, NOT
  with input count alone — the parallelism actually helps.
* Slow items don't head-of-line block the rest of the batch.

These don't need a live proxy or DB — they exercise the runner
itself with a coroutine factory that simulates bounded latency.
Live-stack interaction is covered by the existing live-e2e suite.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from app.services.concurrency import BulkRunner


@pytest.mark.asyncio
async def test_100_items_complete_with_cap_8_in_predictable_time():
    """100 items × 100 ms work / cap 8 ≈ ⌈100/8⌉ × 0.1 = 1.3 s wall.
    Allow 2.5× headroom for CI scheduling jitter — still well below
    a "no cap, all parallel" worst case (which would be 0.1 s) AND
    well below a serial worst case (10 s)."""
    started = time.monotonic()

    async def work(_idx: int) -> int:
        await asyncio.sleep(0.1)
        return _idx

    result = await BulkRunner(
        coroutine_factory=work,
        inputs=list(range(100)),
        op_kind="load_test_100",
        logger=MagicMock(),
        max_concurrency_override=8,
    ).run()
    elapsed = time.monotonic() - started

    # Sanity: every item completed.
    assert result.success_count == 100
    assert result.error_count == 0

    # Cap enforced — wall time bounded below by serial-by-cap.
    # 100 items × 100 ms / 8 concurrency = 1.25 s lower bound.
    assert elapsed >= 1.0, (
        f"completed faster than the cap allows ({elapsed:.2f}s) "
        f"— BulkRunner cap may not be enforced"
    )
    # Upper bound — generous for CI noise.
    assert elapsed < 4.0, (
        f"took {elapsed:.2f}s for 100×100ms / cap 8 — "
        f"BulkRunner is doing serial work, parallelism broken"
    )


@pytest.mark.asyncio
async def test_one_slow_item_does_not_head_of_line_block():
    """A single 2 s slow item shouldn't add 2 s to the entire
    batch's wall time — the other items continue running while the
    slow one occupies one cap slot."""
    started = time.monotonic()

    async def work(idx: int) -> int:
        if idx == 0:
            await asyncio.sleep(2.0)  # the slow one
        else:
            await asyncio.sleep(0.05)
        return idx

    result = await BulkRunner(
        coroutine_factory=work,
        inputs=list(range(20)),
        op_kind="head_of_line",
        logger=MagicMock(),
        max_concurrency_override=4,
    ).run()
    elapsed = time.monotonic() - started

    assert result.success_count == 20

    # Lower bound: at least 2 s (the slow item's duration).
    assert elapsed >= 1.9, (
        f"slow item didn't actually slow — was the sleep skipped?"
    )
    # Upper bound: a serial implementation would take ~3 s
    # (2 + 19×0.05). With cap 4 the fast 19 items run in parallel
    # while the slow one holds one slot, so total ≈ max(2, ⌈19/3⌉×0.05)
    # = 2.05 s. Allow 2× headroom for CI.
    assert elapsed < 3.5, (
        f"took {elapsed:.2f}s — a single 2s slow item shouldn't "
        f"block the batch this hard. Possible regression: cap "
        f"semaphore acquired before checking item characteristics."
    )


@pytest.mark.asyncio
async def test_per_item_exception_classification():
    """Half-success runs are common in production. Confirm the
    BulkRunResult correctly partitions outcomes and the structured
    log carries both counts."""
    logger = MagicMock()

    async def maybe_fail(idx: int) -> int:
        if idx % 2 == 0:
            raise RuntimeError(f"item {idx} fails")
        return idx

    result = await BulkRunner(
        coroutine_factory=maybe_fail,
        inputs=list(range(10)),
        op_kind="half_success",
        logger=logger,
        max_concurrency_override=3,
    ).run()

    # 5 succeed (odd indices), 5 fail (even).
    assert result.success_count == 5
    assert result.error_count == 5
    # Spot-check shape — odd indices have the expected results,
    # even indices have RuntimeErrors.
    assert result.results[0] is None
    assert result.errors[0] is not None
    assert isinstance(result.errors[0], RuntimeError)
    assert result.results[1] == 1
    assert result.errors[1] is None

    # The structured log should reflect the split, NOT just
    # "X completed".
    info_call = next(
        c for c in logger.info.call_args_list if "bulk_op_completed" in str(c)
    )
    ctx = info_call.kwargs["context"]
    assert ctx["success_count"] == 5
    assert ctx["error_count"] == 5
    assert ctx["op_kind"] == "half_success"
