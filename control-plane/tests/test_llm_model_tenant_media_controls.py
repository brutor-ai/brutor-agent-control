# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tenant-scope MEDIA controls on ``llm_model``.

The tenant-wide limit/governance blobs (``tenant_limits`` /
``tenant_governance``) are opaque ``Dict[str, Any]`` JSON columns. For a
media-mode model (embedding, image_generation, audio_speech, …) the blob
carries that mode's own limit/governance keys. This test proves the whole
persistence + response surface (ORM column -> ``to_dict()`` ->
``LLModelResponse``) carries the media-mode keys verbatim — i.e. the blob is
genuinely mode-agnostic and no media key is silently dropped (the recurring
"Pydantic strips undeclared fields" gotcha would bite if the response model
ever stopped declaring the blob).

Follows the harness in ``test_llm_model_tenant_controls.py``.

Run: pytest tests/test_llm_model_tenant_media_controls.py -q
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.models.admin.llm_model import LLModelResponse
from app.database import Base
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider


# ---- Base per-mode limit keys (shared by every media mode) -------------------
BASE_LIMIT_KEYS = {
    "daily_budget_usd": 5.0,
    "monthly_budget_usd": 100.0,
    "daily_requests": 500,
    "monthly_requests": 10000,
    "daily_warning_percent": 80,
    "monthly_warning_percent": 90,
    "hard_stop": True,
}

# ---- Per-mode media key sets -------------------------------------------------
EMBEDDING_LIMITS = {
    **BASE_LIMIT_KEYS,
    "daily_input_tokens": 1_000_000,
    "monthly_input_tokens": 30_000_000,
}
EMBEDDING_GOV = {
    "dim_whitelist": [256, 512, 1536],
    "max_batch_size": 96,
    "max_input_tokens_per_item": 8192,
}

IMAGE_LIMITS = {
    **BASE_LIMIT_KEYS,
    "daily_images": 200,
    "monthly_images": 5000,
}
IMAGE_GOV = {
    "size_whitelist": ["1024x1024", "512x512"],
    "quality_whitelist": ["standard", "hd"],
    "style_whitelist": ["vivid", "natural"],
    "max_n": 4,
    "max_prompt_characters": 4000,
}


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Full create_all — LLModel.to_dict() touches the provider relationship
    # + emits the health/performance blocks; the conftest JSONB->TEXT shim
    # keeps it SQLite-safe.
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    s.add(LlmProvider(id="prov-x", tenant_id="t1", key="openai", display_name="OpenAI"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_model(db: Session, *, model_id: str, mode: str, limits: dict, gov: dict) -> LLModel:
    m = LLModel(
        id=model_id, tenant_id="t1", name=model_id.upper(),
        provider_id="prov-x", model_name="m", mode=mode,
        tenant_limits=limits, tenant_governance=gov,
    )
    db.add(m)
    db.commit()
    return db.query(LLModel).filter(LLModel.id == model_id).first()


def _round_trip_response(m: LLModel) -> LLModelResponse:
    """ORM -> to_dict() -> LLModelResponse, the real GET response surface.

    The list/detail response builder in the admin endpoints layers a few
    request-shaped fields (auth_type, default_temperature, …) on top of
    to_dict(); supply valid stand-ins here so the round-trip focuses on the
    tenant_limits/tenant_governance blobs, which is what this test guards.
    """
    d = m.to_dict()
    d.setdefault("api_url_auto_discovered", False)
    d.setdefault("auth_type", "api_key")
    d.setdefault("default_temperature", 0.7)
    d.setdefault("default_max_tokens", 1024)
    d.setdefault("routing_weight", 1)
    d.setdefault("timeout", 30.0)
    return LLModelResponse(**d)


def test_response_declares_tenant_control_blobs():
    """Regression guard for the strip-undeclared class: the opaque blobs must
    be declared on the response model or every media key vanishes on GET."""
    fields = LLModelResponse.model_fields
    assert "tenant_limits" in fields
    assert "tenant_governance" in fields


def test_embedding_mode_media_keys_round_trip(db):
    m = _seed_model(
        db, model_id="llm-embed", mode="embedding",
        limits=EMBEDDING_LIMITS, gov=EMBEDDING_GOV,
    )

    # to_dict carries the blob verbatim.
    d = m.to_dict()
    assert d["tenant_limits"] == EMBEDDING_LIMITS
    assert d["tenant_governance"] == EMBEDDING_GOV

    # Full response-model round-trip preserves every key.
    resp = _round_trip_response(m)
    assert resp.tenant_limits == EMBEDDING_LIMITS
    assert resp.tenant_governance == EMBEDDING_GOV

    # Explicit per-key assertions so a partial drop is caught by name.
    for key, val in EMBEDDING_LIMITS.items():
        assert resp.tenant_limits[key] == val, f"limits.{key} dropped"
    for key, val in EMBEDDING_GOV.items():
        assert resp.tenant_governance[key] == val, f"governance.{key} dropped"


def test_image_generation_mode_media_keys_round_trip(db):
    m = _seed_model(
        db, model_id="llm-image", mode="image_generation",
        limits=IMAGE_LIMITS, gov=IMAGE_GOV,
    )

    d = m.to_dict()
    assert d["tenant_limits"] == IMAGE_LIMITS
    assert d["tenant_governance"] == IMAGE_GOV

    resp = _round_trip_response(m)
    assert resp.tenant_limits == IMAGE_LIMITS
    assert resp.tenant_governance == IMAGE_GOV

    for key, val in IMAGE_LIMITS.items():
        assert resp.tenant_limits[key] == val, f"limits.{key} dropped"
    for key, val in IMAGE_GOV.items():
        assert resp.tenant_governance[key] == val, f"governance.{key} dropped"


def test_blob_is_mode_agnostic_survives_reload(db):
    """Both mode blobs coexist across two rows without cross-contamination —
    the column is a per-row opaque blob, not a fixed-schema struct."""
    _seed_model(db, model_id="llm-embed", mode="embedding",
                limits=EMBEDDING_LIMITS, gov=EMBEDDING_GOV)
    _seed_model(db, model_id="llm-image", mode="image_generation",
                limits=IMAGE_LIMITS, gov=IMAGE_GOV)

    embed = db.query(LLModel).filter(LLModel.id == "llm-embed").first()
    image = db.query(LLModel).filter(LLModel.id == "llm-image").first()

    assert embed.tenant_limits == EMBEDDING_LIMITS
    assert image.tenant_limits == IMAGE_LIMITS
    # media-mode-specific keys don't leak across rows
    assert "daily_images" not in embed.tenant_limits
    assert "daily_input_tokens" not in image.tenant_limits
