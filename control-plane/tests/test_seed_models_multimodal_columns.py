# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression test: ``init_llms.create_llm_models`` actually persists the
top-level multimodal columns from the provider seeds.

Why this exists
---------------
The multimodal migrations promoted 13 columns off the
``capabilities`` JSONB blob and onto top-level columns
(``cost_per_image``, ``supported_image_sizes``, ``cost_per_audio_char``,
``supported_voices``, ``cost_per_audio_minute``,
``supported_input_formats``, ``cost_per_video_second``,
``supported_video_resolutions`` etc.) so the proxy's request-time
validators can enforce them without parsing JSON.

But the seed pipeline grew over many phases — the
``LLModel(...)`` constructor inside ``create_llm_models`` is a hand-
maintained list of ``column=llm_data.get(...)`` lines. It's easy to
add a column to the migration + seed dict and forget to thread it
through this constructor. The result is a silent failure: the column
exists in the DB, the seed dict has the value, but the row lands with
NULL because the constructor dropped the dict key on the floor. The
proxy validator falls through to "no whitelist" and accepts whatever
the caller sent.

This test catches that regression by:

  1. Spinning up an in-memory SQLite session with the LLModel +
     Tenant + ResourceGroup tables.
  2. Calling ``create_llm_models`` for a synthetic tenant.
  3. Picking out one model per multimodal mode (image / audio_speech /
     audio_transcription / video_generation) and asserting the new
     columns are populated.

If a future migration adds another multimodal column, extend the
``EXPECTED_COLUMNS_BY_MODE`` map and this test fails until the seed
loader is updated to thread it through.
"""

from __future__ import annotations

import os

# Standard test env shim — keeps `app.database.__init__` happy.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone
from typing import Iterable

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.init_llms import create_llm_models
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
)
from app.database.models.tenant import Tenant


from app.database.models.llm_catalog import LlmCatalog

REQUIRED_TABLES = [
    # LlmProvider must come first — LLModel.provider_id FKs into it.
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    # LlmCatalog before LLModel — LLModel.catalog_id FKs into it and
    # create_llm_models now seeds the global catalog first.
    LlmCatalog.__table__,
    LLModel.__table__,
    Tenant.__table__,
    ResourceGroup.__table__,
    resource_group_llm_models,
]


# Per-mode whitelist of columns the seed loader is expected to
# thread through. If a column is added in a future phase, append
# it here — the test fails until the loader (and at least one
# seed) populate it.
EXPECTED_COLUMNS_BY_MODE: dict[str, set[str]] = {
    "image_generation": {
        "cost_per_image",
        "supported_image_sizes",
        "supported_image_qualities",
        "max_images_per_request",
    },
    "audio_speech": {
        "cost_per_audio_char",
        "supported_voices",
        "supported_audio_formats",
    },
    "audio_transcription": {
        "cost_per_audio_minute",
        "supported_input_formats",
        # `max_audio_duration_seconds` is optional at the seed
        # level (operators often want NULL = "no cap"), so we
        # don't pin it here.
    },
    "video_generation": {
        "cost_per_video_second",
        "supported_video_resolutions",
        "supported_video_durations",
        "max_video_duration_seconds",
    },
}


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # FKs intentionally OFF — the create_llm_models pipeline
    # touches a tangle of tables (tenants, resource_groups,
    # resource_group_llm_models, llm_model). We exercise the
    # column-population contract here, not referential integrity.
    @event.listens_for(engine, "connect")
    def _disable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)

    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_tenant_and_group(s) -> tuple[str, str]:
    """Insert a minimal tenant + default group and return their ids."""
    tenant_id = "tenant-test-multimodal"
    group_id = "rg-test-default"

    t = Tenant(
        id=tenant_id,
        name=tenant_id,
        display_name="Test",
        organization_name="Test Org",
        primary_contact_email="a@test.example",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
    )
    g = ResourceGroup(
        id=group_id,
        tenant_id=tenant_id,
        name="default",
        display_name="Default",
        parent_group_id=None,
        hierarchy_level=0,
        hierarchy_path="default",
        inherit_resources=True,
        is_active=True,
    )
    s.add_all([t, g])
    s.flush()
    return tenant_id, group_id


def _models_for_mode(s, tenant_id: str, mode: str) -> list[LLModel]:
    return list(
        s.query(LLModel)
        .filter(LLModel.tenant_id == tenant_id, LLModel.mode == mode)
        .all()
    )


def _assert_some_row_has_all_columns(
    rows: Iterable[LLModel], required: set[str], mode: str
) -> None:
    """At least one row for the mode must have every required column
    populated. We do "at least one" rather than "every" because some
    seeds intentionally leave fields NULL (e.g. ollama embedding
    models with no per-request cap)."""
    rows = list(rows)
    assert rows, f"no rows seeded for mode={mode!r}"

    populated_per_column: dict[str, list[str]] = {col: [] for col in required}
    for row in rows:
        for col in required:
            if getattr(row, col) is not None:
                populated_per_column[col].append(row.id)

    missing = [col for col, ids in populated_per_column.items() if not ids]
    assert not missing, (
        f"After create_llm_models() ran, no {mode} row populated "
        f"these columns: {sorted(missing)}. The seed dict probably "
        f"set them but the LLModel(...) constructor in "
        f"`init_llms.create_llm_models` dropped the dict key on the "
        f"floor. Check the constructor list against "
        f"EXPECTED_COLUMNS_BY_MODE."
    )


def _also_enable(monkeypatch, model_name: str) -> None:
    """Seed one more catalog model enabled, beside the default-enabled ones."""
    from app.database import init_llms

    real = init_llms.get_default_llm_configurations

    def configurations():
        rows = real()
        hit = [r for r in rows if r["model_name"] == model_name]
        assert hit, f"{model_name} is not in the seed"
        for r in hit:
            r["enabled"] = True
        return rows

    monkeypatch.setattr(init_llms, "get_default_llm_configurations", configurations)


def test_image_generation_seeds_populate_top_level_columns(session, monkeypatch):
    # The default-enabled image model (gpt-image-2.6-flare) is priced per
    # token: OpenAI publishes no per-image price for it, so its
    # `cost_per_image` is empty on purpose. The contract held here is that
    # `create_llm_models` threads every image column from the seed dict to
    # the row, `cost_per_image` included, so a seeded image model that does
    # have a per-image price is enabled beside the default one.
    _also_enable(monkeypatch, "gemini-nano-banana-2.1")
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    rows = _models_for_mode(session, tenant_id, "image_generation")
    _assert_some_row_has_all_columns(
        rows,
        EXPECTED_COLUMNS_BY_MODE["image_generation"],
        "image_generation",
    )


def test_audio_speech_seeds_populate_top_level_columns(session):
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    rows = _models_for_mode(session, tenant_id, "audio_speech")
    _assert_some_row_has_all_columns(
        rows,
        EXPECTED_COLUMNS_BY_MODE["audio_speech"],
        "audio_speech",
    )


def test_audio_transcription_seeds_populate_top_level_columns(session):
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    rows = _models_for_mode(session, tenant_id, "audio_transcription")
    _assert_some_row_has_all_columns(
        rows,
        EXPECTED_COLUMNS_BY_MODE["audio_transcription"],
        "audio_transcription",
    )


def test_video_generation_seeds_populate_top_level_columns(session):
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    rows = _models_for_mode(session, tenant_id, "video_generation")
    _assert_some_row_has_all_columns(
        rows,
        EXPECTED_COLUMNS_BY_MODE["video_generation"],
        "video_generation",
    )


def test_default_baseline_image_model_is_gpt_image_2_6_flare(session):
    """Sanity check: gpt-image-2.6-flare is enabled and lands in the default
    group. The baseline has moved with OpenAI's retirements:
    dall-e-3 → gpt-image-1-mini → gpt-image-2 → gpt-image-2.6-flare, each
    shut down by OpenAI in turn. If this
    regresses, the trial bundle's image-generation guide stops being honest."""
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    img_rows = _models_for_mode(session, tenant_id, "image_generation")
    enabled = [r for r in img_rows if r.enabled]
    names = {r.model_name for r in enabled}
    assert names == {"gpt-image-2.6-flare"}, (
        f"gpt-image-2.6-flare should be the one default-enabled image "
        f"baseline; enabled image-mode models were: {sorted(names)}"
    )
    gpt_image = next(r for r in enabled if r.model_name == "gpt-image-2.6-flare")
    # Priced per token (text in $5, image out $30 per 1M): OpenAI publishes
    # no per-image price for the 2.6 models, so none is seeded.
    assert gpt_image.cost_per_image is None
    assert gpt_image.cost_per_input_token == 5.0
    assert gpt_image.cost_per_output_token == 30.0
    assert gpt_image.capabilities["cost_per_image_input_token"] == 8.0
    assert gpt_image.supported_image_sizes == ["1024x1024", "1536x1024", "1024x1536", "auto"]
    assert gpt_image.supported_image_qualities == ["low", "medium", "high", "xhigh", "max", "auto"]
    assert gpt_image.max_images_per_request == 10

    # DALL-E is retired — both names must be gone
    # from the seed catalog. Defensive guard against accidental
    # re-add via a stale merge.
    assert "dall-e-3" not in names, "dall-e-3 was retired — should not be seeded"
    assert "dall-e-2" not in names, "dall-e-2 was retired — should not be seeded"


def test_replicate_seeds_register_and_load(session):
    """Replicate is a seeded provider. Make sure it actually
    flows through PROVIDER_REGISTRY → seed_llm_catalog.

    Replicate models are disabled-by-default catalog entries, so after the
    catalog refactor they register in the GLOBAL `llm_catalog` (not as
    per-tenant `llm_model` instances). create_llm_models seeds the catalog
    as a side effect."""
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    replicate_rows = (
        session.query(LlmCatalog)
        .filter(LlmCatalog.provider_key == "replicate")
        .all()
    )
    assert replicate_rows, (
        "no Replicate catalog rows persisted — check that "
        "replicate_models_init is in PROVIDER_REGISTRY."
    )
    # Image mode is the smoke check that the whole pipeline
    # (seed → loader → catalog) cohered.
    flux = next(
        (r for r in replicate_rows if "flux-schnell" in r.model_name),
        None,
    )
    assert flux is not None, "Replicate flux-schnell seed missing"
    assert flux.mode == "image_generation"


def test_assemblyai_seeds_register_and_load(session):
    """AssemblyAI is a seeded provider. Disabled-by-default → registers
    in the GLOBAL catalog after the catalog refactor."""
    tenant_id, group_id = _seed_tenant_and_group(session)
    create_llm_models(
        db=session,
        tenant_id=tenant_id,
        default_group_id=group_id,
        logger=BrutorLogger("test"),
    )
    rows = (
        session.query(LlmCatalog)
        .filter(LlmCatalog.provider_key == "assemblyai")
        .all()
    )
    assert rows, (
        "no AssemblyAI catalog rows persisted — check that "
        "assemblyai_models_init is in PROVIDER_REGISTRY."
    )
    best = next((r for r in rows if r.model_name == "universal-3-5-pro"), None)
    assert best is not None
    assert best.mode == "audio_transcription"
