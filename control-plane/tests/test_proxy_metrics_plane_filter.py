# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Governed / Observed / All plane filter for Mission Control.

`proxy_metrics.source` discriminates governed inline traffic ('gateway')
from observed imported telemetry ('observed'). The dashboard's plane toggle
maps onto this column via `source_for_plane`. These tests seed real
ProxyMetricEntry rows on SQLite and assert get_performance_metrics splits the
counts/cost correctly per plane.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.proxy_metric import ProxyMetricEntry
from app.services.proxy_metrics_service import (
    UnifiedProxyMetricsService,
    source_for_plane,
)

TENANT = "t-plane"
REQUIRED = [ProxyMetricEntry.__table__]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _row(rid: str, source: str, cost: float, tokens: int) -> ProxyMetricEntry:
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    return ProxyMetricEntry(
        id=rid,
        tenant_id=TENANT,
        timestamp=ts,
        proxy_type="llm",
        source=source,
        request_id=rid,
        end_user_type="imported" if source == "observed" else "api_key",
        http_method="IMPORT" if source == "observed" else "POST",
        total_duration_ms=0.0 if source == "observed" else 120.0,
        success=True,
        request_hour=12,
        request_day_of_week=0,
        llm_data={
            "prompt_tokens": tokens,
            "completion_tokens": 0,
            "total_tokens": tokens,
            "estimated_cost_usd": cost,
        },
    )


@pytest.fixture()
def service():
    svc = UnifiedProxyMetricsService(logger=BrutorLogger("test-plane"))
    # A2A/media read proxy_logs (not seeded here) — stub to empties.
    svc._aggregate_a2a = MagicMock(return_value=({}, [], {
        "total_requests": 0, "successful_requests": 0, "success_rate": 0.0,
        "avg_duration_ms": 0.0, "total_data_transferred_mb": 0.0,
    }))
    svc._aggregate_media_usage = MagicMock(return_value={
        "request_count": 0, "estimated_cost_usd": 0.0, "modes": {},
    })
    return svc


@pytest.fixture()
def seeded(db):
    db.add_all([
        _row("g1", "gateway", 1.00, 100),
        _row("g2", "gateway", 2.00, 200),
        _row("o1", "observed", 10.00, 1000),
    ])
    db.commit()
    return db


def test_source_for_plane_mapping():
    assert source_for_plane("governed") == "gateway"
    assert source_for_plane("observed") == "observed"
    assert source_for_plane("all") is None
    assert source_for_plane(None) is None
    assert source_for_plane("bogus") is None


@pytest.mark.asyncio
async def test_plane_all_counts_everything(seeded, service):
    r = await service.get_performance_metrics(seeded, tenant_id=TENANT, period_hours=0)
    assert r["overview"]["total_requests"] == 3
    assert r["llm_economics"]["estimated_cost_usd"] == pytest.approx(13.00)
    assert r["llm_economics"]["total_tokens"] == 1300


@pytest.mark.asyncio
async def test_plane_governed_excludes_observed(seeded, service):
    r = await service.get_performance_metrics(
        seeded, tenant_id=TENANT, period_hours=0, plane="governed"
    )
    assert r["overview"]["total_requests"] == 2
    assert r["llm_economics"]["estimated_cost_usd"] == pytest.approx(3.00)
    assert r["llm_economics"]["total_tokens"] == 300


@pytest.mark.asyncio
async def test_plane_observed_only(seeded, service):
    r = await service.get_performance_metrics(
        seeded, tenant_id=TENANT, period_hours=0, plane="observed"
    )
    assert r["overview"]["total_requests"] == 1
    assert r["llm_economics"]["estimated_cost_usd"] == pytest.approx(10.00)
    assert r["llm_economics"]["total_tokens"] == 1000
    # observed plane must not invoke the proxy_logs aggregators (governed-only)
    service._aggregate_a2a.assert_not_called()
    service._aggregate_media_usage.assert_not_called()
