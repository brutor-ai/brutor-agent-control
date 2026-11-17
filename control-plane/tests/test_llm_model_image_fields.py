# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the image-generation columns added to ``llm_model``.

The multimodal model elevates four image fields off the
``capabilities`` JSONB blob and onto top-level columns:

  * ``cost_per_image``           — USD per generated image
  * ``supported_image_sizes``    — whitelist of accepted sizes
  * ``supported_image_qualities`` — whitelist of accepted qualities
  * ``max_images_per_request``   — `n` parameter cap

These tests pin down three things that would silently regress if the
schema, ORM model, and Pydantic surface ever drift:

  1. The four columns are present on ``LLModel.__table__`` with the
     expected types and nullability. Without this, the migration ran
     in the live DB but the ORM doesn't expose the columns — admin
     UI saves a value, the round-trip silently drops it.
  2. ``LLModelCreate`` / ``LLModelUpdate`` / ``LLModelResponse``
     accept and serialise the new fields. The Pydantic Response model
     uses ``from_attributes = True``, so a missing field would *strip*
     it on every API read — the classic stripped-field bug called out
     in CLAUDE.md.
  3. ``to_dict`` includes the four fields. The Rust gateway-core proxy
     reads model rows directly from Postgres (not via this ``to_dict``)
     but the seed-init pipeline uses it for diff reporting; missing
     fields here cause "model unchanged" misreports on every restart.

Run: pytest tests/test_llm_model_image_fields.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import Float, Integer, JSON

from app.api.v1.models.admin.llm_model import (
    LLModelCreate,
    LLModelResponse,
    LLModelUpdate,
)
from app.database.models.llm_model import LLModel


# ---------------------------------------------------------------------------
# 1. SQLAlchemy column shape
# ---------------------------------------------------------------------------

def test_image_columns_present_on_llm_model_table():
    """Every image column has to live on the ORM table — otherwise
    the migration ran but the ORM doesn't see it, and admin UI writes
    silently drop the value."""
    cols = {c.name: c for c in LLModel.__table__.columns}
    expected = {
        "cost_per_image",
        "supported_image_sizes",
        "supported_image_qualities",
        "max_images_per_request",
    }
    missing = expected - cols.keys()
    assert not missing, (
        f"LLModel ORM is missing image columns: {sorted(missing)}. "
        "Live DB migration may have run but the model class wasn't "
        "updated — admin UI writes will silently drop these fields."
    )


def test_cost_per_image_is_float_and_nullable():
    """Matches the rest of the cost columns (DOUBLE PRECISION /
    nullable). NUMERIC with explicit precision was considered and
    rejected in the migration docstring — Decimal types create a
    Pydantic coercion gap that we don't want to paper over."""
    col = LLModel.__table__.columns["cost_per_image"]
    assert isinstance(col.type, Float), (
        f"cost_per_image should be Float to match cost_per_input_token "
        f"and cost_per_output_token; got {type(col.type).__name__}"
    )
    assert col.nullable is True, "Non-image models leave this NULL"


def test_supported_image_sizes_is_json_and_nullable():
    """Generic ``JSON`` (not ``JSONB``) on the ORM side keeps SQLite
    test fixtures working. The migration physically creates the column
    as ``JSONB`` in Postgres — the migration choice doesn't bind the
    ORM type."""
    col = LLModel.__table__.columns["supported_image_sizes"]
    assert isinstance(col.type, JSON), (
        f"supported_image_sizes should be generic JSON for "
        f"cross-dialect test compatibility; got "
        f"{type(col.type).__name__}"
    )
    assert col.nullable is True


def test_supported_image_qualities_is_json_and_nullable():
    col = LLModel.__table__.columns["supported_image_qualities"]
    assert isinstance(col.type, JSON)
    assert col.nullable is True


def test_max_images_per_request_is_non_null_with_default_one():
    """All models — image or not — get a value here so the proxy can
    do `n_requested > model.max_images_per_request` without a NULL
    branch. Default 1 matches DALL-E 3's hard cap."""
    col = LLModel.__table__.columns["max_images_per_request"]
    assert isinstance(col.type, Integer)
    assert col.nullable is False, (
        "max_images_per_request must be NOT NULL so the proxy can "
        "validate `n` without a special case for chat/embedding rows."
    )
    # ScalarElementColumnDefault(1) — read the wrapped value
    assert col.default is not None
    assert col.default.arg == 1


# ---------------------------------------------------------------------------
# 2. Pydantic surface
# ---------------------------------------------------------------------------

def test_llmodel_create_accepts_image_fields():
    """The schema lands the image fields on Create so the admin UI can
    POST them when a tenant adds a new image-mode model."""
    payload = LLModelCreate(
        name="dall-e-3",
        provider_id="prov-test",
        model_name="dall-e-3",
        mode="image_generation",
        cost_per_image=0.04,
        supported_image_sizes=["1024x1024", "1024x1792", "1792x1024"],
        supported_image_qualities=["standard", "hd"],
        max_images_per_request=1,
    )
    dumped = payload.model_dump()
    assert dumped["cost_per_image"] == 0.04
    assert dumped["supported_image_sizes"] == [
        "1024x1024",
        "1024x1792",
        "1792x1024",
    ]
    assert dumped["supported_image_qualities"] == ["standard", "hd"]
    assert dumped["max_images_per_request"] == 1


def test_llmodel_create_image_fields_default_to_chat_friendly_values():
    """Chat / embedding models don't have to pass image fields. The
    defaults must not poison their rows: cost_per_image stays None,
    the lists stay None, and max_images_per_request is 1 (the proxy
    only checks this column when mode='image_generation')."""
    payload = LLModelCreate(
        name="gpt-4",
        provider_id="prov-test",
        model_name="gpt-4",
        mode="chat",
    )
    dumped = payload.model_dump()
    assert dumped["cost_per_image"] is None
    assert dumped["supported_image_sizes"] is None
    assert dumped["supported_image_qualities"] is None
    # NOT NULL on the DB side; default 1 is fine for non-image rows
    assert dumped["max_images_per_request"] == 1


def test_llmodel_update_image_fields_are_partial():
    """Update is patch-style — only fields explicitly set should
    appear in the dump. Otherwise an admin updating the cost
    accidentally clears the size whitelist."""
    update = LLModelUpdate(supported_image_qualities=["hd"])
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"supported_image_qualities": ["hd"]}


def test_llmodel_update_rejects_too_many_images_per_request():
    """`n` cap of 10 matches OpenAI's largest documented batch. Going
    higher silently means an upstream provider rejection — fail fast
    at the admin form instead."""
    with pytest.raises(ValueError):
        LLModelUpdate(max_images_per_request=42)


def test_llmodel_update_rejects_negative_cost_per_image():
    """Negative cost would credit the tenant on every image — clearly
    not intended; we want a 4xx, not a "free image generation" bug."""
    with pytest.raises(ValueError):
        LLModelUpdate(cost_per_image=-0.01)


def test_llmodel_response_includes_image_fields_in_schema():
    """LLModelResponse uses ``from_attributes = True`` to pull from
    SQLAlchemy. If any of the four fields is missing from the
    response schema, every API read silently drops the value — the
    "Pydantic strips undeclared fields" gotcha called out in
    CLAUDE.md.

    This test asserts the field is on the model's schema, not that a
    round-trip preserves it; round-trip is exercised by the live
    integration suite once the migration has run.
    """
    fields = LLModelResponse.model_fields
    for name in (
        "cost_per_image",
        "supported_image_sizes",
        "supported_image_qualities",
        "max_images_per_request",
    ):
        assert name in fields, (
            f"LLModelResponse is missing `{name}` — "
            "Pydantic will strip it on every API read."
        )


# ---------------------------------------------------------------------------
# 3. to_dict surfaces the new fields
# ---------------------------------------------------------------------------

def test_to_dict_surfaces_image_fields():
    """``LLModel.to_dict`` is used by the seed-init diff reporter. If
    it doesn't include the new fields, every restart will misreport
    those columns as "unchanged" even when an admin updated them —
    and the cost/size whitelist will silently revert to the seed
    value on the next provisioning sweep."""
    model = LLModel(
        id="llm-test",
        tenant_id="t-test",
        name="DALL-E 3",
        provider_id="prov-test",
        model_name="dall-e-3",
        mode="image_generation",
        cost_per_image=0.04,
        supported_image_sizes=["1024x1024", "1024x1792"],
        supported_image_qualities=["standard", "hd"],
        max_images_per_request=1,
    )
    d = model.to_dict()
    assert d["cost_per_image"] == 0.04
    assert d["supported_image_sizes"] == ["1024x1024", "1024x1792"]
    assert d["supported_image_qualities"] == ["standard", "hd"]
    assert d["max_images_per_request"] == 1
    # Sanity — mode is also surfaced (image-mode rows would otherwise be
    # indistinguishable from chat rows in the seed-diff path).
    assert d["mode"] == "image_generation"
