# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
BulkRunner — a shared bounded-fanout primitive for control-plane
admin endpoints that issue many requests through the proxy.

Why this exists
===============

Control-plane endpoints like ``bulk_health_check_models`` and
``bulk_health_check`` (MCP) used to fan out via raw
``asyncio.gather(*tasks)``. With 70+ seeded models, that's 70+
simultaneous chat completions hitting the proxy. The proxy's
default DB pool of 20 connections cannot service that — pool
acquire saturates, every request hits the 5 s acquire timeout,
the cascade surfaces as ReadTimeouts on ``_reset_proxy_resilience``
and 502s on chat completions.

Each admin endpoint that owns its own ``Semaphore(N)``
re-introduces the same bug if N is wrong. **One shared primitive
keeps the cap correct everywhere by deriving it from the proxy's
known pool ceiling.**

Concurrency cap derivation
==========================

::

    cap = max(2, floor(BRUTOR_GATEWAY_DB_POOL_SIZE * (1 - DEFAULT_POOL_RESERVE)))

* ``BRUTOR_GATEWAY_DB_POOL_SIZE`` (env, default 20) — the actual proxy pool
  ceiling. Reading the proxy's value here is the whole point — if
  the proxy is sized to 60, the cap rises to 36 instead of staying
  at the per-endpoint hardcoded 8.
* ``DEFAULT_POOL_RESERVE`` (0.4, fixed) — fraction of the
  pool reserved for organic traffic *during* a bulk sweep so a
  tenant's chat traffic doesn't get starved by an admin-initiated
  health check.

Per-call ``max_concurrency_override`` is supported for cases where
each unit holds more than one connection — e.g. probes that fire
both a chat completion AND a follow-up reset-resilience.

Telemetry
=========

Every ``BulkRunner.run()`` emits a structured ``bulk_op_completed``
log with ``op_kind``, ``total_items``, ``concurrency``,
``duration_ms``, ``success_count``, ``error_count``. Drives the
"Bulk operations" Loki dashboard panel.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from app.core.logging_system import BrutorLogger
from app.env_vars import BRUTOR_GATEWAY_DB_POOL_SIZE


T = TypeVar("T")
"""Type of each input item passed to the coroutine factory."""
R = TypeVar("R")
"""Type of the per-item result."""


# Reserve fraction. 0.4 means we use 60% of the pool for the bulk op
# and leave 40% for organic traffic.
DEFAULT_POOL_RESERVE: float = 0.4


def derive_concurrency_cap(
    proxy_pool_size: Optional[int] = None,
    pool_reserve: Optional[float] = None,
    floor: int = 2,
    ceiling: int = 64,
) -> int:
    """Compute the bounded-fanout cap.

    Parameters chosen so the cap *cannot* exceed proxy pool capacity:
    a ``pool_reserve`` close to 0 would
    saturate the proxy; a value close to 1 would under-utilise.
    Both extremes get clamped by the floor + ceiling defaults.

    Reading the proxy's pool size from env at *every* call (vs.
    capturing once at import) keeps the cap responsive to operator
    knob changes without a control-plane restart — useful when an
    operator scales the proxy pool to handle launch traffic.
    """
    if proxy_pool_size is None:
        proxy_pool_size = BRUTOR_GATEWAY_DB_POOL_SIZE.integer()
    if pool_reserve is None:
        pool_reserve = DEFAULT_POOL_RESERVE

    # Sanity-clamp the reserve so a typo can't disable bounding.
    if pool_reserve < 0.0:
        pool_reserve = 0.0
    if pool_reserve > 0.95:
        pool_reserve = 0.95

    raw = int(proxy_pool_size * (1.0 - pool_reserve))
    return max(floor, min(ceiling, raw))


@dataclass
class BulkRunResult(Generic[T, R]):
    """Outcome of a bulk run.

    ``results`` and ``errors`` are aligned with ``inputs`` by index —
    callers that need stable order across both lists can iterate
    ``zip(inputs, results, errors)`` and skip rows where the
    counterpart is None.
    """

    inputs: List[T]
    results: List[Optional[R]]
    errors: List[Optional[BaseException]]
    duration_ms: float
    concurrency: int

    @property
    def success_count(self) -> int:
        return sum(1 for r, e in zip(self.results, self.errors) if e is None)

    @property
    def error_count(self) -> int:
        return sum(1 for e in self.errors if e is not None)

    @property
    def total(self) -> int:
        return len(self.inputs)


class BulkRunner(Generic[T, R]):
    """Bounded-fanout helper.

    Construct once per call site (it's cheap), call ``await run()``,
    inspect ``BulkRunResult`` for per-item outcomes.

    ``coroutine_factory`` is invoked with each input and must return
    a coroutine. Exceptions raised by the coroutine are captured per-
    item rather than bubbling up — bulk ops should never have one
    bad item halt the rest.

    ``op_kind`` is a free-form string used for the structured
    completion log (``llm_health_check``, ``mcp_health_check``,
    etc.). Stable label.

    ``max_concurrency_override`` lets a caller cap below the derived
    ceiling when each unit is heavier than one DB connection. Going
    *above* the derived ceiling is intentionally not supported —
    that's the whole reason this primitive exists.
    """

    def __init__(
        self,
        coroutine_factory: Callable[[T], Awaitable[R]],
        inputs: Iterable[T],
        op_kind: str,
        logger: BrutorLogger,
        max_concurrency_override: Optional[int] = None,
        proxy_pool_size: Optional[int] = None,
        pool_reserve: Optional[float] = None,
    ) -> None:
        self._factory = coroutine_factory
        self._inputs: List[T] = list(inputs)
        self._op_kind = op_kind
        self._logger = logger

        derived = derive_concurrency_cap(
            proxy_pool_size=proxy_pool_size,
            pool_reserve=pool_reserve,
        )
        if max_concurrency_override is not None:
            # Tighten only — never widen above the derived ceiling.
            self._concurrency = max(1, min(max_concurrency_override, derived))
        else:
            self._concurrency = derived

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def total_items(self) -> int:
        return len(self._inputs)

    async def run(self) -> BulkRunResult[T, R]:
        """Execute ``coroutine_factory(item)`` for every input, with
        bounded concurrency. Always returns a :class:`BulkRunResult`;
        a coroutine raising is captured into ``errors[i]`` instead of
        propagating.
        """
        sem = asyncio.Semaphore(self._concurrency)
        results: List[Optional[R]] = [None] * len(self._inputs)
        errors: List[Optional[BaseException]] = [None] * len(self._inputs)

        async def _run_one(idx: int, item: T) -> None:
            async with sem:
                try:
                    results[idx] = await self._factory(item)
                except BaseException as exc:  # noqa: BLE001 — capture all
                    # KeyboardInterrupt / SystemExit are intentionally
                    # captured too: a bulk admin op shouldn't propagate
                    # a process-fatal signal mid-fan-out — that risks
                    # leaving half the items un-finalized. Final loop
                    # exits cleanly via gather.
                    errors[idx] = exc

        started = time.monotonic()
        tasks = [
            asyncio.create_task(_run_one(idx, item))
            for idx, item in enumerate(self._inputs)
        ]
        # gather with return_exceptions=False is safe here because
        # _run_one swallows everything.
        await asyncio.gather(*tasks)
        duration_ms = (time.monotonic() - started) * 1000.0

        out = BulkRunResult(
            inputs=self._inputs,
            results=results,
            errors=errors,
            duration_ms=duration_ms,
            concurrency=self._concurrency,
        )

        # Structured completion log — Loki / Grafana friendly.
        self._logger.info(
            "bulk_op_completed",
            context={
                "op_kind": self._op_kind,
                "total_items": out.total,
                "concurrency": out.concurrency,
                "duration_ms": int(out.duration_ms),
                "success_count": out.success_count,
                "error_count": out.error_count,
            },
        )

        return out
