# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the model-level ``fallback_models`` column on ``llm_model``.

Model-level failover was re-introduced: an ordered list of model ids to try, in
order, when THIS model's own call fails with a retryable error (the
industry-standard model→model failover chain). These tests pin the schema, ORM,
Pydantic surface, and ``to_dict`` so the field can't silently regress — the
classic "Pydantic strips undeclared fields" gotcha in CLAUDE.md would otherwise
drop it on every API read.

Run: pytest tests/test_llm_model_fallback_models.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from sqlalchemy import JSON

from app.api.v1.models.admin.llm_model import (
    LLModelCreate,
    LLModelResponse,
    LLModelUpdate,
)
from app.database.models.llm_model import LLModel


# ---------------------------------------------------------------------------
# 1. SQLAlchemy column shape
# ---------------------------------------------------------------------------

def test_fallback_models_column_present_on_table():
    cols = {c.name: c for c in LLModel.__table__.columns}
    assert "fallback_models" in cols, (
        "LLModel ORM is missing `fallback_models` — migration a122_llm_model_fallback_models may have run "
        "in the live DB but the model class wasn't updated, so admin UI writes "
        "silently drop the failover chain."
    )


def test_fallback_models_is_json_and_nullable():
    """Generic ``JSON`` (not ``JSONB``) keeps SQLite test fixtures working;
    the migration physically creates JSONB in Postgres. Non-failover models
    leave it NULL."""
    col = LLModel.__table__.columns["fallback_models"]
    assert isinstance(col.type, JSON)
    assert col.nullable is True


# ---------------------------------------------------------------------------
# 2. Pydantic surface — Create / Update / Response
# ---------------------------------------------------------------------------

def test_create_accepts_fallback_models():
    payload = LLModelCreate(
        name="sales-haiku",
        provider_id="prov-anthropic",
        model_name="claude-haiku-4-5",
        mode="chat",
        fallback_models=["llm-ops-gpt4o-mini", "llm-sales-gpt5"],
    )
    dumped = payload.model_dump()
    assert dumped["fallback_models"] == ["llm-ops-gpt4o-mini", "llm-sales-gpt5"], (
        "the ordered failover chain must survive Create serialisation, in order"
    )


def test_create_fallback_models_defaults_to_none():
    """A model without a failover chain must not get a poisoned default."""
    payload = LLModelCreate(
        name="gpt-4",
        provider_id="prov-openai",
        model_name="gpt-4",
        mode="chat",
    )
    assert payload.model_dump()["fallback_models"] is None


def test_update_fallback_models_is_partial():
    """Patch-style: setting only fallback_models must not touch other fields."""
    update = LLModelUpdate(fallback_models=["llm-backup"])
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"fallback_models": ["llm-backup"]}


def test_update_can_clear_fallback_models_with_empty_list():
    """An operator removing every fallback sends [] — it must round-trip as an
    empty list (distinct from 'not set'), so the chain is actually cleared."""
    update = LLModelUpdate(fallback_models=[])
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"fallback_models": []}


def test_response_schema_includes_fallback_models():
    """LLModelResponse uses from_attributes=True; a missing field would strip
    the failover chain on every API read."""
    assert "fallback_models" in LLModelResponse.model_fields, (
        "LLModelResponse is missing `fallback_models` — Pydantic will strip it "
        "on every API read, so the admin UI form reverts on reload."
    )


# ---------------------------------------------------------------------------
# 3. to_dict surfaces the field
# ---------------------------------------------------------------------------

def test_to_dict_surfaces_fallback_models():
    model = LLModel(
        id="llm-test",
        tenant_id="t-test",
        name="Sales Haiku",
        provider_id="prov-anthropic",
        model_name="claude-haiku-4-5",
        mode="chat",
        fallback_models=["llm-ops-gpt4o-mini"],
    )
    d = model.to_dict()
    assert d["fallback_models"] == ["llm-ops-gpt4o-mini"]
