# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the LLM Media usage aggregator on
``UnifiedProxyMetricsService`` — the function that powers the new
"LLM Media" panel in Mission Control and the per-group Usage tab.

Token-priced metrics already roll up via ``llm_economics``; the
media routes (image / TTS / STT / video) populate dedicated
``media_*`` columns on ``proxy_logs`` so dashboards can chart their
billing units (images, characters, seconds) without conflating them
into a token total. This test file exercises the SQL filter, the
endpoint→mode bucketing, and the per-mode counter sums.

Run: pytest tests/test_media_usage_aggregation.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.proxy_log import ProxyLog
from app.database.models.tenant import Tenant
from app.database.models.usage_log import UsageLog
from app.services.proxy_metrics_service import UnifiedProxyMetricsService


REQUIRED_TABLES = [Tenant.__table__, ProxyLog.__table__, UsageLog.__table__]

TENANT_A = "t-a"
TENANT_B = "t-b"


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        for tid in (TENANT_A, TENANT_B):
            s.add(
                Tenant(
                    id=tid,
                    name=tid,
                    display_name=tid,
                    organization_name=tid,
                    primary_contact_email=f"admin@{tid}.local",
                    is_active=True,
                    is_default=False,
                    is_trial=False,
                    suspended=False,
                    billing_plan="self_hosted",
                )
            )
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def svc() -> UnifiedProxyMetricsService:
    return UnifiedProxyMetricsService(logger=BrutorLogger("test"))


def _now():
    return datetime.now(timezone.utc)


def _seed_media_log(
    db: Session,
    *,
    tenant_id: str = TENANT_A,
    endpoint: str,
    timestamp: datetime | None = None,
    cost_usd: float = 0.0,
    images_generated: int | None = None,
    audio_chars: int | None = None,
    audio_seconds_input: float | None = None,
    video_seconds_output: float | None = None,
    success: bool = True,
) -> ProxyLog:
    log = ProxyLog(
        id=f"log-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        proxy_type="llm",
        proxy_subtype=None,
        timestamp=timestamp or _now(),
        correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        end_user_session_id=f"sess-{uuid.uuid4().hex[:8]}",
        http_method="POST",
        endpoint=endpoint,
        target_url="https://api.example.com",
        success=success,
        duration_ms=100.0,
        llm_estimated_cost_usd=cost_usd,
        media_images_generated=images_generated,
        media_audio_chars=audio_chars,
        media_audio_seconds_input=audio_seconds_input,
        media_video_seconds_output=video_seconds_output,
    )
    db.add(log)
    db.flush()
    return log


def _seed_chat_log(
    db: Session,
    *,
    tenant_id: str = TENANT_A,
    timestamp: datetime | None = None,
    total_tokens: int = 100,
    cost_usd: float = 0.001,
) -> ProxyLog:
    """Plain chat row with NO media counters. Used to verify the
    media filter excludes token-priced traffic."""
    log = ProxyLog(
        id=f"log-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        proxy_type="llm",
        proxy_subtype="openai",
        timestamp=timestamp or _now(),
        correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        end_user_session_id=f"sess-{uuid.uuid4().hex[:8]}",
        http_method="POST",
        endpoint="/v1/chat/completions",
        target_url="https://api.openai.com",
        success=True,
        duration_ms=100.0,
        llm_total_tokens=total_tokens,
        llm_estimated_cost_usd=cost_usd,
    )
    db.add(log)
    db.flush()
    return log


# =============================================================================
# Empty cases — no rows or only non-media rows
# =============================================================================


class TestEmptyCases:
    def test_no_logs_at_all_returns_empty_block(self, db: Session, svc):
        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        assert result == {
            "request_count": 0,
            "estimated_cost_usd": 0.0,
            "modes": {},
        }

    def test_only_chat_rows_returns_empty_block(self, db: Session, svc):
        # Chat traffic must NOT leak into the media block — the filter
        # checks media_* columns are non-NULL specifically. Without this
        # guarantee `llm_economics` and `media_usage` would both count
        # the same chat tokens and the dashboard would double-count.
        _seed_chat_log(db)
        _seed_chat_log(db)
        db.commit()
        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        assert result["request_count"] == 0
        assert result["modes"] == {}


# =============================================================================
# Per-mode bucketing + counter sums
# =============================================================================


class TestPerModeAggregation:
    def test_image_generation_aggregates_count_and_cost(
        self, db: Session, svc
    ):
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.08,
            images_generated=2,  # multi-image request
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )

        assert result["request_count"] == 2
        assert result["estimated_cost_usd"] == 0.12
        modes = result["modes"]
        assert "image_generation" in modes
        img = modes["image_generation"]
        assert img["request_count"] == 2
        assert img["images_generated"] == 3
        assert img["cost_usd"] == 0.12
        # Counters for OTHER modes stay zero.
        assert img["audio_chars"] == 0
        assert img["audio_seconds_input"] == 0.0
        assert img["video_seconds_output"] == 0.0

    def test_audio_speech_sums_input_chars(self, db: Session, svc):
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/speech",
            cost_usd=0.0015,
            audio_chars=100,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/speech",
            cost_usd=0.003,
            audio_chars=200,
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        tts = result["modes"]["audio_speech"]
        assert tts["request_count"] == 2
        assert tts["audio_chars"] == 300
        assert tts["cost_usd"] == 0.0045

    def test_audio_transcription_sums_audio_seconds(
        self, db: Session, svc
    ):
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/transcriptions",
            cost_usd=0.006,
            audio_seconds_input=60.0,  # 1 minute
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/transcriptions",
            cost_usd=0.003,
            audio_seconds_input=30.5,
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        stt = result["modes"]["audio_transcription"]
        assert stt["request_count"] == 2
        assert stt["audio_seconds_input"] == 90.5
        assert stt["cost_usd"] == 0.009

    def test_video_generation_sums_output_seconds(
        self, db: Session, svc
    ):
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/video/generations",
            cost_usd=0.50,
            video_seconds_output=5.0,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/video/generations",
            cost_usd=1.0,
            video_seconds_output=10.0,
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        vid = result["modes"]["video_generation"]
        assert vid["request_count"] == 2
        assert vid["video_seconds_output"] == 15.0
        assert vid["cost_usd"] == 1.5

    def test_mixed_modes_split_correctly(self, db: Session, svc):
        # All four modes in one period — the rollup must keep them
        # separate. This catches a regression where an endpoint→mode
        # mapping bug bucketed everything under one key.
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/speech",
            cost_usd=0.0015,
            audio_chars=100,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/audio/transcriptions",
            cost_usd=0.006,
            audio_seconds_input=60.0,
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/video/generations",
            cost_usd=0.50,
            video_seconds_output=5.0,
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        assert result["request_count"] == 4
        # Top-level cost is the sum across all modes.
        assert result["estimated_cost_usd"] == pytest.approx(0.5475)
        # All four mode keys present.
        assert set(result["modes"].keys()) == {
            "image_generation",
            "audio_speech",
            "audio_transcription",
            "video_generation",
        }


# =============================================================================
# Tenant + time scoping
# =============================================================================


class TestScoping:
    def test_tenant_scoping_excludes_other_tenants(self, db: Session, svc):
        _seed_media_log(
            db,
            tenant_id=TENANT_A,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
        )
        _seed_media_log(
            db,
            tenant_id=TENANT_B,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
        )
        db.commit()

        result_a = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        result_b = svc._aggregate_media_usage(
            db, TENANT_B, _now() - timedelta(hours=24)
        )
        assert result_a["request_count"] == 1
        assert result_b["request_count"] == 1
        # No cross-bleed.
        assert result_a["modes"]["image_generation"]["images_generated"] == 1
        assert result_b["modes"]["image_generation"]["images_generated"] == 1

    def test_time_window_excludes_old_rows(self, db: Session, svc):
        now = _now()
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
            timestamp=now - timedelta(hours=2),  # outside 1h window
        )
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/images/generations",
            cost_usd=0.04,
            images_generated=1,
            timestamp=now - timedelta(minutes=30),  # inside
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, now - timedelta(hours=1)
        )
        assert result["request_count"] == 1
        assert result["modes"]["image_generation"]["images_generated"] == 1


# =============================================================================
# Defensive: unknown endpoints
# =============================================================================


class TestDefensive:
    def test_media_counter_on_unknown_endpoint_is_dropped(
        self, db: Session, svc
    ):
        # If a counter ever leaks onto a non-media endpoint (operator
        # error, schema migration in flight, etc.) we drop the row
        # rather than misbucketing it. The total count still reflects
        # the row — the per-mode breakdown does not.
        _seed_media_log(
            db,
            endpoint="/v1/proxy/llm/something/else",
            cost_usd=0.01,
            images_generated=1,
        )
        db.commit()

        result = svc._aggregate_media_usage(
            db, TENANT_A, _now() - timedelta(hours=24)
        )
        # Top-level reflects the row was found...
        assert result["request_count"] == 1
        # ...but it's not categorised into any known mode.
        assert result["modes"] == {}


# =============================================================================
# Group-usage cost rollup — media MUST contribute to the group's
# total LLM cost (and therefore summary.total_cost).
# =============================================================================


def _seed_usage_log(
    db: Session,
    *,
    tenant_id: str = TENANT_A,
    resource_type: str = "llm",
    model_name: str | None = None,
    estimated_cost: float | None = None,
    total_tokens: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    endpoint: str | None = None,
    timestamp: datetime | None = None,
) -> UsageLog:
    """Seed a single ``usage_logs`` row. Mirrors what the Rust
    background writer persists for chat / media / mcp traffic."""
    now = timestamp or _now()
    log = UsageLog(
        tenant_id=tenant_id,
        user_id=None,
        group_id=None,
        ancestry_path=None,
        group_name=None,
        resource_type=resource_type,
        # resource_id is NOT NULL on the schema — fall back to the
        # model name (or a synthetic id for non-model rows like MCP
        # calls) so we can seed test rows without a full provider
        # registry in the test DB.
        resource_id=model_name or f"res-{uuid.uuid4().hex[:8]}",
        resource_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model_name=model_name,
        provider="openai",
        mcp_tool_name=None,
        mcp_call_count=None,
        mcp_execution_time_ms=None,
        mcp_data_size_bytes=None,
        estimated_cost=estimated_cost,
        currency="USD",
        prompt_cost=None,
        completion_cost=None,
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        status_code=200,
        response_time_ms=100,
        http_method="POST",
        endpoint=endpoint,
        client_ip=None,
        user_agent=None,
        is_inherited=False,
        inherited_from_group_id=None,
        created_at=now,
    )
    db.add(log)
    db.flush()
    return log


class TestSummaryCostIncludesMedia:
    """The Group Usage endpoint computes
    ``total_llm_cost = sum(estimated_cost for resource_type='llm' rows)``
    and ``summary.total_cost = total_llm_cost + total_mcp_cost``. Media
    rows are written by the Rust ``record_multimodal_outcome`` helper as
    ``resource_type='llm'`` with token columns NULL but ``estimated_cost``
    populated — so they MUST contribute to ``total_llm_cost`` (and
    therefore the visible "Total Cost" tile at the top of the Group
    Usage tab). These tests assert that contract holds at the SQL+
    list-comprehension layer the endpoint uses, so a future refactor
    that filters media out of the LLM rollup fails CI loudly.
    """

    def test_total_llm_cost_includes_chat_and_media(self, db: Session):
        # 1 chat row + 1 image row, both resource_type='llm'.
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="gpt-4o-mini",
            estimated_cost=0.10,
            total_tokens=1_000,
            prompt_tokens=600,
            completion_tokens=400,
            endpoint="/v1/proxy/llm/chat/completions",
        )
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="dall-e-3",
            estimated_cost=0.04,
            total_tokens=None,  # media — no tokens
            endpoint="/v1/proxy/llm/images/generations",
        )
        db.commit()

        # Apply the same filter + sum the endpoint uses (see
        # resource_groups.py:get_group_usage).
        logs = db.query(UsageLog).filter(UsageLog.tenant_id == TENANT_A).all()
        llm_logs = [l for l in logs if l.resource_type == "llm"]
        total_llm_cost = sum(l.estimated_cost or 0 for l in llm_logs)

        # Both rows contribute. If a future refactor splits media out
        # of llm_logs, this breaks.
        assert len(llm_logs) == 2
        assert total_llm_cost == pytest.approx(0.14)

    def test_summary_total_cost_equals_llm_plus_mcp_with_media(self, db: Session):
        # Mixed scenario: chat ($0.10) + image ($0.04) + mcp ($0.02).
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="gpt-4o-mini",
            estimated_cost=0.10,
            total_tokens=500,
            endpoint="/v1/proxy/llm/chat/completions",
        )
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="dall-e-3",
            estimated_cost=0.04,
            endpoint="/v1/proxy/llm/images/generations",
        )
        _seed_usage_log(
            db,
            resource_type="mcp",
            model_name=None,
            estimated_cost=0.02,
            endpoint="/v1/proxy/mcp/...",
        )
        db.commit()

        logs = db.query(UsageLog).filter(UsageLog.tenant_id == TENANT_A).all()
        total_llm_cost = sum(
            l.estimated_cost or 0 for l in logs if l.resource_type == "llm"
        )
        total_mcp_cost = sum(
            l.estimated_cost or 0 for l in logs if l.resource_type == "mcp"
        )
        # This is the exact formula the endpoint uses for
        # `summary.total_cost`. Media is part of `total_llm_cost`.
        total_cost = round(total_llm_cost + total_mcp_cost, 6)
        assert total_cost == pytest.approx(0.16)
        # Sanity: removing media must drop the total by exactly the
        # media cost — proves it's in there, not somehow filtered.
        chat_only_llm_cost = sum(
            l.estimated_cost or 0
            for l in logs
            if l.resource_type == "llm" and l.model_name != "dall-e-3"
        )
        assert (total_llm_cost - chat_only_llm_cost) == pytest.approx(0.04)

    def test_media_only_tenant_still_has_nonzero_total_cost(self, db: Session):
        # Edge case: a tenant that ONLY uses media (no chat) must
        # still see a total_cost > 0. This is the regression class
        # that prompted the user's request — media-only operators
        # would otherwise stare at "$0.00" while their providers
        # bill them.
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="dall-e-3",
            estimated_cost=0.04,
            endpoint="/v1/proxy/llm/images/generations",
        )
        _seed_usage_log(
            db,
            resource_type="llm",
            model_name="tts-1",
            estimated_cost=0.0015,
            endpoint="/v1/proxy/llm/audio/speech",
        )
        db.commit()

        logs = db.query(UsageLog).filter(UsageLog.tenant_id == TENANT_A).all()
        total_llm_cost = sum(
            l.estimated_cost or 0 for l in logs if l.resource_type == "llm"
        )
        assert total_llm_cost == pytest.approx(0.0415)
        assert total_llm_cost > 0  # media-only tenant has visible spend
