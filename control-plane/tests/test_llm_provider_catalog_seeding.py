# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Provider Catalog — seeding refactor tests

  * seed_llm_provider_catalog populates llm_provider_catalog GLOBALLY and
    idempotently (re-run = no dupes, update-in-place).
  * After the refactor, create_llm_models does NOT bulk-seed all ~41
    `llm_providers` rows per tenant — a fresh tenant gets only the
    providers its enabled seed models actually use.
  * upsert_provider_for_key materializes a provider from the catalog with
    catalog_id set + is_system for builtin, and falls back to a minimal
    stub for an unknown slug.
"""

import os

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
from app.database.init_llm_providers import (
    PROVIDER_SEED_CATALOG,
    seed_llm_provider_catalog,
    upsert_provider_for_key,
)
from app.database.init_llms import (
    create_llm_models,
    get_default_llm_configurations,
)
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import (
    LlmProviderCatalog,
    seeded_provider_catalog_id,
)
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
    return BrutorLogger("test-provider-catalog-seed")


class TestSeedProviderCatalog:
    def test_seed_populates_catalog_globally(self, db, logger):
        inserted = seed_llm_provider_catalog(db, logger=logger)
        total = db.execute(
            select(func.count()).select_from(LlmProviderCatalog)
        ).scalar_one()
        assert inserted == len(PROVIDER_SEED_CATALOG)
        assert total == len(PROVIDER_SEED_CATALOG)
        # No tenant_id on the catalog rows — it's GLOBAL.
        assert not hasattr(LlmProviderCatalog, "tenant_id") or (
            "tenant_id" not in LlmProviderCatalog.__table__.columns
        )

    def test_seed_is_idempotent(self, db, logger):
        first = seed_llm_provider_catalog(db, logger=logger)
        total_after_first = db.execute(
            select(func.count()).select_from(LlmProviderCatalog)
        ).scalar_one()
        second = seed_llm_provider_catalog(db, logger=logger)
        total_after_second = db.execute(
            select(func.count()).select_from(LlmProviderCatalog)
        ).scalar_one()
        assert first > 0
        assert second == 0  # re-run inserts nothing (updates in place)
        assert total_after_first == total_after_second

    def test_entries_have_key_and_deterministic_id(self, db, logger):
        seed_llm_provider_catalog(db, logger=logger)
        rows = db.execute(select(LlmProviderCatalog)).scalars().all()
        for r in rows:
            assert r.key
            assert r.display_name
            assert r.id == seeded_provider_catalog_id(r.key)
            assert r.source == "builtin"

    def test_icon_blob_seeded_for_known_provider(self, db, logger):
        seed_llm_provider_catalog(db, logger=logger)
        openai = db.get(LlmProviderCatalog, seeded_provider_catalog_id("openai"))
        assert openai is not None
        # OpenAI ships a bundled icon.
        assert openai.has_icon()
        assert openai.icon_data is not None
        assert openai.icon_hash


class TestFreshTenantNoBulkProviders:
    def test_fresh_tenant_gets_no_bulk_providers(self, db, logger):
        """A fresh tenant must NOT receive all ~41 llm_providers rows."""
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        provider_count = db.execute(
            select(func.count()).select_from(LlmProvider).where(
                LlmProvider.tenant_id == "t1"
            )
        ).scalar_one()
        # Only the providers used by enabled seed models are materialized —
        # strictly fewer than the full catalogue.
        assert 0 < provider_count < len(PROVIDER_SEED_CATALOG)

    def test_materialized_providers_match_used_models(self, db, logger):
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        used_provider_ids = {
            m.provider_id
            for m in db.execute(select(LLModel)).scalars().all()
        }
        provider_ids = {
            p.id
            for p in db.execute(
                select(LlmProvider).where(LlmProvider.tenant_id == "t1")
            ).scalars().all()
        }
        # Every materialized provider is referenced by at least one model
        # (no orphan bulk rows) and every model's provider exists.
        assert provider_ids == used_provider_ids

    def test_materialized_providers_carry_catalog_id(self, db, logger):
        create_llm_models(
            db=db, tenant_id="t1",
            default_group_id="group-t1-default", logger=logger,
        )
        catalog_ids = {
            c.id
            for c in db.execute(select(LlmProviderCatalog)).scalars().all()
        }
        providers = db.execute(
            select(LlmProvider).where(LlmProvider.tenant_id == "t1")
        ).scalars().all()
        for p in providers:
            # Seeded providers came from the catalog → catalog_id + is_system.
            assert p.catalog_id is not None
            assert p.catalog_id in catalog_ids
            assert p.is_system is True


class TestUpsertProviderForKey:
    def test_materializes_one_provider_from_catalog(self, db, logger):
        seed_llm_provider_catalog(db, logger=logger)
        prov = upsert_provider_for_key(
            db, tenant_id="t1", key="anthropic", is_system=True,
        )
        assert prov.key == "anthropic"
        assert prov.catalog_id == seeded_provider_catalog_id("anthropic")
        assert prov.is_system is True
        # Copied descriptive metadata + icon off the catalog row.
        assert prov.display_name == "Anthropic"
        assert prov.icon_data is not None
        # Only ONE provider materialized — not the whole catalogue.
        total = db.execute(
            select(func.count()).select_from(LlmProvider).where(
                LlmProvider.tenant_id == "t1"
            )
        ).scalar_one()
        assert total == 1

    def test_idempotent_returns_existing(self, db, logger):
        seed_llm_provider_catalog(db, logger=logger)
        a = upsert_provider_for_key(db, tenant_id="t1", key="openai")
        b = upsert_provider_for_key(db, tenant_id="t1", key="openai")
        assert a.id == b.id
        total = db.execute(
            select(func.count()).select_from(LlmProvider).where(
                LlmProvider.tenant_id == "t1"
            )
        ).scalar_one()
        assert total == 1

    def test_unknown_slug_falls_back_to_stub(self, db, logger):
        seed_llm_provider_catalog(db, logger=logger)
        prov = upsert_provider_for_key(
            db, tenant_id="t1", key="some_custom_vendor",
        )
        assert prov.key == "some_custom_vendor"
        assert prov.catalog_id is None
        assert prov.display_name == "Some Custom Vendor"
