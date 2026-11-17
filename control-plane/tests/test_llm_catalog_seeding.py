# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog — seeding refactor tests

  * seed_llm_catalog populates llm_catalog idempotently (re-run = no dupes)
  * after the refactor, create_llm_models imports ONLY the default-enabled
    seeds as configured llm_model instances with catalog_id set, and does
    NOT create the disabled catalog rows as llm_model rows.
"""

import os
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database.models  # noqa: F401 — register full metadata
from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.init_llms import (
    create_llm_models,
    get_default_llm_configurations,
    seed_llm_catalog,
)
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    s.add(Tenant(id="t1", name="t1", display_name="T1",
                 primary_contact_email="a@t1.example"))
    s.flush()
    s.add(ResourceGroup(id="group-t1-default", tenant_id="t1", name="Default",
                        group_type=GroupType.CUSTOM, is_system=True,
                        is_active=True))
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def logger():
    return BrutorLogger("test-catalog-seed")


class TestSeedCatalog:
    def test_seed_populates_catalog(self, db, logger):
        inserted = seed_llm_catalog(db, logger=logger)
        total = db.execute(
            select(func.count()).select_from(LlmCatalog)
        ).scalar_one()
        assert inserted > 0
        assert total == inserted
        # Every source config maps to exactly one catalog row (unique on
        # provider_key + model_name; the source list may contain dup keys
        # only if two seeds collide — assert no collision blew up the count
        # beyond the distinct set).
        configs = get_default_llm_configurations()
        distinct = {
            ((c.get("provider") or "").lower(), c.get("model_name"))
            for c in configs
            if c.get("provider") and c.get("model_name")
        }
        assert total == len(distinct)

    def test_seed_is_idempotent(self, db, logger):
        first = seed_llm_catalog(db, logger=logger)
        total_after_first = db.execute(
            select(func.count()).select_from(LlmCatalog)
        ).scalar_one()
        second = seed_llm_catalog(db, logger=logger)
        total_after_second = db.execute(
            select(func.count()).select_from(LlmCatalog)
        ).scalar_one()
        assert first > 0
        assert second == 0  # re-run inserts nothing (updates in place)
        assert total_after_first == total_after_second

    def test_catalog_entries_have_provider_and_mode(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(select(LlmCatalog)).scalars().all()
        for r in rows:
            assert r.provider_key
            assert r.model_name
            assert r.mode
            assert r.id == f"llmcat-{r.provider_key}-{_slug(r.model_name)}"


class TestCatalogImportDefaults:
    """The per-model import-default columns must be seeded with the
    same non-null values the seed dicts carry, so import_to_model can copy
    them onto an imported instance (the import regression fix)."""

    def test_image_model_carries_image_whitelist(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(
            select(LlmCatalog).where(LlmCatalog.mode == "image_generation")
        ).scalars().all()
        assert rows, "expected at least one image catalog entry"
        # At least one image model declares an image-size whitelist + per
        # image cost (the seed source sets these).
        with_sizes = [r for r in rows if r.supported_image_sizes]
        assert with_sizes, "no image catalog entry carried supported_image_sizes"
        r = with_sizes[0]
        assert isinstance(r.supported_image_sizes, list)
        assert r.max_images_per_request is not None
        assert any(x.cost_per_image is not None for x in rows)

    def test_audio_tts_model_carries_voices(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(
            select(LlmCatalog).where(LlmCatalog.mode == "audio_speech")
        ).scalars().all()
        assert rows, "expected at least one TTS catalog entry"
        with_voices = [r for r in rows if r.supported_voices]
        assert with_voices, "no TTS catalog entry carried supported_voices"
        assert isinstance(with_voices[0].supported_voices, list)

    def test_audio_stt_model_carries_minute_cost(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(
            select(LlmCatalog).where(LlmCatalog.mode == "audio_transcription")
        ).scalars().all()
        assert rows, "expected at least one STT catalog entry"
        assert any(r.cost_per_audio_minute is not None for r in rows)

    def test_video_model_carries_resolutions(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(
            select(LlmCatalog).where(LlmCatalog.mode == "video_generation")
        ).scalars().all()
        assert rows, "expected at least one video catalog entry"
        with_res = [r for r in rows if r.supported_video_resolutions]
        assert with_res, "no video catalog entry carried supported_video_resolutions"
        assert any(r.cost_per_video_second is not None for r in rows)

    def test_reasoning_model_carries_drop_params(self, db, logger):
        """A reasoning model seed sets additional_drop_params (strips
        temperature/top_p). The catalog must carry it forward."""
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(select(LlmCatalog)).scalars().all()
        with_adp = [r for r in rows if r.additional_drop_params]
        assert with_adp, "no catalog entry carried additional_drop_params"
        assert isinstance(with_adp[0].additional_drop_params, list)

    def test_parameters_and_tags_carried(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = db.execute(select(LlmCatalog)).scalars().all()
        assert any(r.parameters is not None for r in rows)
        assert any(r.tags for r in rows)

    def test_import_defaults_idempotent_refresh(self, db, logger):
        """Re-seed refreshes the import-default columns in place (no dupes,
        values preserved)."""
        seed_llm_catalog(db, logger=logger)
        before = {
            r.id: r.supported_image_sizes
            for r in db.execute(select(LlmCatalog)).scalars().all()
            if r.supported_image_sizes
        }
        seed_llm_catalog(db, logger=logger)
        after = {
            r.id: r.supported_image_sizes
            for r in db.execute(select(LlmCatalog)).scalars().all()
            if r.supported_image_sizes
        }
        assert before == after and before


def _slug(model_name):
    from app.database.models.llm_catalog import slug_model_name
    return slug_model_name(model_name)


class TestCreateLlmModelsRefactor:
    def test_imports_only_enabled_as_instances(self, db, logger):
        llm_ids = create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )

        # Every imported instance count equals the number of default-enabled
        # seeds (modulo the build_model_config retire-date force-disable,
        # which can only DECREASE enabled count). We assert the instances
        # are a subset of the enabled seeds.
        enabled_seeds = [
            c for c in get_default_llm_configurations() if c.get("enabled")
        ]
        enabled_names = {c["model_name"] for c in enabled_seeds}

        instances = db.execute(select(LLModel)).scalars().all()
        assert len(instances) == len(llm_ids)
        assert len(instances) > 0
        # No disabled-only catalog rows leaked in as instances.
        for m in instances:
            assert m.model_name in enabled_names

    def test_instances_carry_catalog_id(self, db, logger):
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        instances = db.execute(select(LLModel)).scalars().all()
        catalog_ids = {
            r.id for r in db.execute(select(LlmCatalog)).scalars().all()
        }
        for m in instances:
            assert m.catalog_id is not None
            # provenance points at a real, seeded catalog row
            assert m.catalog_id in catalog_ids

    def test_global_catalog_seeded_as_side_effect(self, db, logger):
        # create_llm_models seeds the GLOBAL catalog once before importing.
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        catalog_total = db.execute(
            select(func.count()).select_from(LlmCatalog)
        ).scalar_one()
        instance_total = db.execute(
            select(func.count()).select_from(LLModel)
        ).scalar_one()
        # Catalog is the FULL set; instances are only the enabled subset.
        assert catalog_total > instance_total

    def test_disabled_seeds_not_materialised(self, db, logger):
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        # Pick a disabled seed model_name and assert no llm_model row for it.
        disabled = [
            c for c in get_default_llm_configurations()
            if not c.get("enabled")
        ]
        assert disabled, "expected at least one disabled seed in the catalog"
        instance_names = {
            m.model_name
            for m in db.execute(select(LLModel)).scalars().all()
        }
        # A disabled model_name that is NOT also an enabled seed must be
        # absent from instances.
        enabled_names = {
            c["model_name"]
            for c in get_default_llm_configurations() if c.get("enabled")
        }
        disabled_only = {
            c["model_name"] for c in disabled
        } - enabled_names
        assert disabled_only, "expected disabled-only model names"
        assert disabled_only.isdisjoint(instance_names)


class TestExplicitCapabilitiesWin:
    """A seed entry's explicit `capabilities` flags outrank the provider
    template — xAI's multi-agent model is a closed agentic system that
    takes no caller tools, and the seed says so. (Found on Groq's compound
    models, which Groq shut down in September 2026 and the seed no longer has.)"""

    def test_build_model_config_honours_explicit_false(self):
        from app.database.init_llms import (
            _catalog_capabilities_from_seed,
            build_model_config,
        )

        cfg = build_model_config(
            "groq",
            {
                "model_name": "groq/compound",
                "name": "Groq Compound",
                "capabilities": {"supports_function_calling": False},
            },
        )
        assert cfg["supports_function_calling"] is False
        caps = _catalog_capabilities_from_seed(cfg)
        assert caps["supports_function_calling"] is False

    def test_seeded_catalog_rows_carry_the_explicit_flags(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = {
            r.model_name: r
            for r in db.execute(select(LlmCatalog)).scalars().all()
            if r.model_name in (
                "grok-4.20-multi-agent-0309", "gemini-nano-banana-2.1",
                "kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed",
            )
        }
        for name in ("grok-4.20-multi-agent-0309", "gemini-nano-banana-2.1"):
            assert rows[name].capabilities.get("supports_function_calling") is False, name
        for name in ("kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed"):
            assert rows[name].capabilities.get("temperature_locked") is True, name


class TestDeprecatedMeansAnnouncedOrNear:
    """`deprecated` on a catalog row: the provider has announced the
    retirement, or the retirement date is inside the horizon. A date alone,
    far off, is a published end of life and not a deprecation: Azure
    publishes a retirement date for every model on the day it launches."""

    TODAY = date(2026, 11, 11)

    def _dep(self, **row):
        from app.database.init_llms import catalog_deprecated

        return catalog_deprecated(row, today=self.TODAY)

    def test_no_date_is_not_deprecated(self):
        assert self._dep() is False

    def test_a_far_date_alone_is_not_deprecated(self):
        assert self._dep(retire_date=date(2028, 1, 11)) is False

    def test_an_announced_retirement_is_deprecated_whatever_the_date(self):
        assert self._dep(
            retire_date=date(2028, 1, 11), retirement_announced_at=date(2026, 10, 1)
        ) is True

    def test_a_date_inside_the_horizon_is_deprecated(self):
        from app.database.init_llms import DEPRECATION_HORIZON_DAYS

        edge = self.TODAY + timedelta(days=DEPRECATION_HORIZON_DAYS)
        assert self._dep(retire_date=edge) is True
        assert self._dep(retire_date=edge + timedelta(days=1)) is False

    def test_a_past_date_is_deprecated(self):
        assert self._dep(retire_date=date(2026, 1, 14)) is True

    def test_the_seeded_rows(self, db, logger):
        seed_llm_catalog(db, logger=logger)
        rows = {
            (r.provider_key, r.model_name): r
            for r in db.execute(select(LlmCatalog)).scalars().all()
        }
        # Announced by OpenAI in October 2026; retirement date: 2027-01-06.
        assert rows[("openai", "tts-1")].deprecated is True
        # Published dates after the horizon, nothing announced.
        for key in (
            ("azure", "azure/gpt-5.6-sol"),
            ("azure", "azure/text-embedding-3-small"),
            ("gemini", "gemini-embedding-001"),
        ):
            assert rows[key].retire_date is not None, key
            assert rows[key].deprecated is False, key
