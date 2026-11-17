# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the LLM economics aggregation in
`UnifiedProxyMetricsService.get_performance_metrics`.

The aggregation reads `proxy_metrics.llm_data` JSON per row and
produces the headline numbers Mission Control's Operations tab
shows under "LLM Economics" (Token Usage / Estimated Cost / Avg
Cost / Avg Tokens). Two contracts pinned here:

  1. **Total tokens equals prompt + completion** whenever at least
     one component is present, never the row-level `total_tokens`
     field. The Rust writer at `routes/llm.rs:664` produces a NULL
     row-level total when either component is None (embeddings,
     mid-stream chunks), and naively summing those NULLs used to
     under-count — Mission Control showed Total < Prompt, the
     numbers stopped reconciling with the per-group view.

  2. **Row-level `total_tokens` is only used when both components
     are missing.** Covers any future provider that records a
     single token count without a prompt/completion split.

The aggregation is tested by mocking the SQLAlchemy `query.filter`
chain — we don't need a real DB to exercise the per-row math.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from app.core.logging_system import BrutorLogger
from app.services.proxy_metrics_service import UnifiedProxyMetricsService


# ────────────────────────────────────────────────────────────────────
# Lightweight stand-in for ProxyMetricEntry. The aggregator only
# touches a small slice of fields; we model exactly those so tests
# don't drag in the SQLAlchemy schema.
# ────────────────────────────────────────────────────────────────────


@dataclass
class FakeMetric:
    success: bool = True
    total_duration_ms: Optional[float] = 100.0
    request_size_bytes: Optional[int] = None
    response_size_bytes: Optional[int] = None
    cache_hit: bool = False
    error_category: Optional[str] = None
    proxy_type: Optional[str] = "llm"
    server_id: Optional[str] = None
    request_hour: int = 12
    end_user_type: str = "api_key"
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    mcp_data: Optional[Dict[str, Any]] = None
    llm_data: Optional[Dict[str, Any]] = None


def _llm(
    *,
    prompt: Optional[int] = None,
    completion: Optional[int] = None,
    total: Optional[int] = None,
    cost: Optional[float] = None,
) -> FakeMetric:
    return FakeMetric(llm_data={
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost_usd": cost,
    })


# ────────────────────────────────────────────────────────────────────
# Real in-memory SQLite. These tests used to run against a FakeDB stub
# that replayed `.query().filter().all()` — which only worked while the
# aggregator loaded whole entities and looped in Python. The aggregation
# is SQL-side now (it was 31s at All-Time on a year of data), so the
# tests exercise the real queries — including the JSON extraction — on
# the same dialect the rest of the unit suite uses.
# ────────────────────────────────────────────────────────────────────

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.proxy_metric import ProxyMetricEntry

REQUIRED = [ProxyMetricEntry.__table__]
_ids = itertools.count(1)


def _make_db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _materialise(db: Session, metrics: List[FakeMetric]) -> None:
    for m in metrics:
        rid = f"row-{next(_ids)}"
        db.add(ProxyMetricEntry(
            id=rid,
            tenant_id="t-test",
            timestamp=m.timestamp,
            proxy_type=m.proxy_type or "llm",
            source="gateway",
            request_id=rid,
            end_user_type=m.end_user_type,
            http_method="POST",
            total_duration_ms=m.total_duration_ms if m.total_duration_ms is not None else 0.0,
            success=m.success,
            request_hour=m.request_hour,
            request_day_of_week=0,
            request_size_bytes=m.request_size_bytes,
            response_size_bytes=m.response_size_bytes,
            cache_hit=m.cache_hit,
            error_category=m.error_category,
            server_id=m.server_id,
            mcp_data=m.mcp_data,
            llm_data=m.llm_data,
        ))
    db.commit()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def service() -> UnifiedProxyMetricsService:
    return UnifiedProxyMetricsService(logger=BrutorLogger("test-proxy-metrics-economics"))


async def _aggregate(
    service: UnifiedProxyMetricsService,
    metrics: List[FakeMetric],
    period_hours: int = 24,
):
    db = _make_db()
    _materialise(db, metrics)
    # Stub the two side aggregations so we don't need fixtures for
    # A2A or media data — they're tested elsewhere. They receive the
    # `start_time` lower bound (None for all-time) — assert that here
    # so the all-time NameError regression can't creep back in.
    service._aggregate_a2a = MagicMock(return_value=({}, [], {
        "total_requests": 0,
        "successful_requests": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0.0,
        "total_data_transferred_mb": 0.0,
    }))
    service._aggregate_media_usage = MagicMock(return_value={
        "request_count": 0,
        "estimated_cost_usd": 0.0,
        "modes": {},
    })
    return await service.get_performance_metrics(
        db, tenant_id="t-test", period_hours=period_hours
    )


# ====================================================================
# Tests
# ====================================================================


class TestLLMEconomics:
    @pytest.mark.asyncio
    async def test_total_equals_prompt_plus_completion_when_both_present(self, service):
        # Standard chat completion. Prompt + completion = total.
        # Pins the headline scenario from the bug report —
        # 55,801 prompt + 963 completion should produce
        # 56,764 total, not whatever the row-level total field
        # happens to say.
        result = await _aggregate(service, [
            _llm(prompt=55801, completion=963, total=12351, cost=0.50),
        ])
        econ = result["llm_economics"]
        assert econ["prompt_tokens"] == 55801
        assert econ["completion_tokens"] == 963
        assert econ["total_tokens"] == 56764  # NOT 12351

    @pytest.mark.asyncio
    async def test_total_handles_null_completion_correctly(self, service):
        # Embedding-style row: prompt populated, completion + total
        # are NULL because the Rust writer's `prompt.zip(completion)`
        # collapses to None when either is missing. Used to be
        # silently dropped from the total. Now: total counts the
        # prompt portion.
        result = await _aggregate(service, [
            _llm(prompt=1000, completion=None, total=None, cost=0.001),
        ])
        econ = result["llm_economics"]
        assert econ["prompt_tokens"] == 1000
        assert econ["completion_tokens"] == 0
        assert econ["total_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_total_handles_null_prompt_correctly(self, service):
        # Belt-and-braces inverse case: completion populated,
        # prompt missing. (Unusual in practice — would imply a
        # streaming chunk that recorded only the output side —
        # but the aggregator should handle it symmetrically.)
        result = await _aggregate(service, [
            _llm(prompt=None, completion=500, total=None, cost=0.002),
        ])
        econ = result["llm_economics"]
        assert econ["prompt_tokens"] == 0
        assert econ["completion_tokens"] == 500
        assert econ["total_tokens"] == 500

    @pytest.mark.asyncio
    async def test_aggregates_across_multiple_rows(self, service):
        # Three rows: one full chat step, one embedding (prompt-only),
        # one streaming chunk pair the writer never reconciled.
        # Mission Control should sum to a coherent total.
        result = await _aggregate(service, [
            _llm(prompt=100, completion=50, total=150, cost=0.01),
            _llm(prompt=2000, completion=None, total=None, cost=0.002),
            _llm(prompt=80, completion=20, total=None, cost=0.005),
        ])
        econ = result["llm_economics"]
        assert econ["prompt_tokens"] == 2180
        assert econ["completion_tokens"] == 70
        assert econ["total_tokens"] == 2250  # = 2180 + 70
        assert econ["llm_request_count"] == 3

    @pytest.mark.asyncio
    async def test_falls_back_to_row_total_when_both_components_missing(self, service):
        # A row with only `total_tokens` set (no per-side
        # breakdown). Possible from a future provider whose
        # response includes a single token count. We must not lose
        # those tokens — fall back to the row's total field.
        result = await _aggregate(service, [
            _llm(prompt=None, completion=None, total=300, cost=0.003),
        ])
        econ = result["llm_economics"]
        assert econ["prompt_tokens"] == 0
        assert econ["completion_tokens"] == 0
        assert econ["total_tokens"] == 300

    @pytest.mark.asyncio
    async def test_skips_rows_with_no_llm_signal(self, service):
        # An MCP / OAuth / portal row passing through the same
        # aggregator (because we filter ProxyMetricEntry by
        # tenant, not proxy_type). Without any of the four
        # signal fields it must not bump `llm_request_count`.
        result = await _aggregate(service, [
            FakeMetric(proxy_type="mcp", llm_data=None),
            FakeMetric(proxy_type="oauth", llm_data={}),
            _llm(prompt=10, completion=5, total=15, cost=0.001),
        ])
        econ = result["llm_economics"]
        assert econ["llm_request_count"] == 1
        assert econ["total_tokens"] == 15

    @pytest.mark.asyncio
    async def test_all_time_period_does_not_zero_out(self, service):
        # Regression: period_hours=0 ("All Time") used to leave
        # `start_time` unbound, raising NameError that the broad
        # except swallowed → every KPI returned 0 while the
        # timeseries/heatmap (which compute their own bound) still
        # rendered data. Assert the all-time path actually aggregates.
        result = await _aggregate(service, [
            _llm(prompt=100, completion=50, total=150, cost=0.50),
            _llm(prompt=2000, completion=None, total=None, cost=0.002),
        ], period_hours=0)
        assert result["overview"]["total_requests"] == 2
        econ = result["llm_economics"]
        assert econ["total_tokens"] == 2150
        assert econ["llm_request_count"] == 2
        # The side aggregators must have been called with start_time=None.
        assert service._aggregate_a2a.call_args.args[2] is None
        assert service._aggregate_media_usage.call_args.args[2] is None

    @pytest.mark.asyncio
    async def test_avg_tokens_uses_corrected_total(self, service):
        # Avg should be derived from the corrected total, not from
        # the broken row-level totals — otherwise the avg would
        # have under-counted alongside the headline.
        result = await _aggregate(service, [
            _llm(prompt=1000, completion=None, total=None, cost=0.001),
            _llm(prompt=500, completion=100, total=None, cost=0.001),
        ])
        econ = result["llm_economics"]
        # Total tokens 1600 / 2 requests = 800 avg
        assert econ["avg_tokens_per_request"] == 800
