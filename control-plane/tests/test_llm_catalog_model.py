# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog — model unit tests

to_dict completeness, mode/source @validates, deterministic seeded id,
and the (provider_key, model_name) unique constraint.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.llm_catalog import (
    CATALOG_MODES,
    LlmCatalog,
    seeded_catalog_id,
    slug_model_name,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[LlmCatalog.__table__])
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


def _entry(**kw):
    base = dict(
        id="llmcat-openai-gpt-4o",
        provider_key="openai",
        model_name="gpt-4o",
        display_name="GPT-4o",
        mode="chat",
    )
    base.update(kw)
    return LlmCatalog(**base)


class TestToDict:
    def test_to_dict_has_every_column(self, db):
        row = _entry(
            description="Flagship",
            capabilities={"supports_vision": True},
            context_window=128000,
            max_output_tokens=4096,
            default_cost_per_input_token=0.0025,
            default_cost_per_output_token=0.01,
            icon_url="https://x/openai.svg",
            recommended=True,
        )
        db.add(row)
        db.commit()
        d = row.to_dict()
        expected = {
            "id", "provider_key", "model_name", "display_name", "description",
            "mode", "capabilities", "context_window", "max_output_tokens",
            "default_cost_per_input_token", "default_cost_per_output_token",
            "icon_url", "recommended", "deprecated", "source",
            "created_at", "updated_at",
            # per-model import-default columns
            "cost_per_image", "supported_image_sizes",
            "supported_image_qualities", "max_images_per_request",
            "cost_per_audio_char", "cost_per_audio_minute",
            "supported_voices", "supported_audio_formats",
            "supported_input_formats", "supported_response_formats",
            "max_audio_duration_seconds", "cost_per_video_second",
            "supported_video_resolutions", "supported_video_durations",
            "max_video_duration_seconds", "thinking_enabled",
            "additional_drop_params", "parameters", "tags", "retire_date",
            # computed, not a column: the gateway's destination rule
            "requires_api_url",
        }
        assert set(d) == expected
        assert d["requires_api_url"] is False
        assert d["provider_key"] == "openai"
        assert d["capabilities"] == {"supports_vision": True}
        assert d["recommended"] is True
        # defaults
        assert d["source"] == "builtin"
        assert d["deprecated"] is False


class TestValidators:
    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            _entry(mode="not-a-mode")

    def test_all_modes_accepted(self):
        for m in CATALOG_MODES:
            _entry(id=f"c-{m}", mode=m)  # no raise

    def test_invalid_source_rejected(self):
        with pytest.raises(ValueError):
            _entry(source="bogus")

    def test_custom_source_accepted(self):
        _entry(source="custom")


class TestUniqueConstraint:
    def test_duplicate_provider_model_rejected(self, db):
        db.add(_entry(id="a"))
        db.commit()
        db.add(_entry(id="b"))  # same (provider_key, model_name)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestDeterministicId:
    def test_slug(self):
        assert slug_model_name("GPT-4o") == "gpt-4o"
        assert slug_model_name("text-embedding-3-small") == "text-embedding-3-small"
        assert slug_model_name("claude.sonnet/4.5") == "claude-sonnet-4-5"

    def test_seeded_id_is_deterministic(self):
        a = seeded_catalog_id("openai", "gpt-4o")
        b = seeded_catalog_id("openai", "gpt-4o")
        assert a == b == "llmcat-openai-gpt-4o"
