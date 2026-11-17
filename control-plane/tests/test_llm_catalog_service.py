# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog — service tests

list/search/filter; get 404; import_to_model creates a real llm_model with
catalog_id + copied defaults + enabled; tenant scoping of the import.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.tenant import Tenant
from app.services.llm_catalog_service import LlmCatalogError, LlmCatalogService


REQUIRED_TABLES = [
    Tenant.__table__,
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLMRoutingGroup.__table__,   # llm_model.routing_group_id FKs here
    LLModel.__table__,
    LlmCatalog.__table__,
]


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

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    for tid in ("t1", "t2"):
        s.add(Tenant(id=tid, name=tid, display_name=tid.upper(),
                     primary_contact_email=f"a@{tid}.example"))
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def svc():
    return LlmCatalogService(BrutorLogger("test-llm-catalog-svc"))


def _seed_catalog(db):
    rows = [
        LlmCatalog(id="llmcat-openai-gpt-4o", provider_key="openai",
                   model_name="gpt-4o", display_name="GPT-4o", mode="chat",
                   recommended=True, context_window=128000,
                   max_output_tokens=4096,
                   default_cost_per_input_token=0.0025,
                   default_cost_per_output_token=0.01,
                   icon_url="https://x/openai.svg",
                   capabilities={"supports_vision": True,
                                 "supports_function_calling": True}),
        LlmCatalog(id="llmcat-openai-text-embedding-3-small",
                   provider_key="openai",
                   model_name="text-embedding-3-small",
                   display_name="Text Embedding 3 Small", mode="embedding",
                   recommended=False),
        LlmCatalog(id="llmcat-anthropic-claude", provider_key="anthropic",
                   model_name="claude-sonnet-4-5",
                   display_name="Claude Sonnet 4.5", mode="chat",
                   recommended=True),
        # An image entry carrying the per-model import-default columns
        # so we can assert import_to_model copies them onto the instance.
        LlmCatalog(id="llmcat-openai-dall-e-3", provider_key="openai",
                   model_name="dall-e-3", display_name="DALL·E 3",
                   mode="image_generation", recommended=False,
                   cost_per_image=0.04,
                   supported_image_sizes=["1024x1024", "1024x1792"],
                   supported_image_qualities=["standard", "hd"],
                   max_images_per_request=1,
                   thinking_enabled=False,
                   additional_drop_params=["top_p"],
                   parameters=12.0,
                   tags=["image", "openai"]),
    ]
    db.add_all(rows)
    db.commit()


class TestList:
    def test_list_all(self, db, svc):
        _seed_catalog(db)
        rows, total = svc.list_catalog(db)
        assert total == 4
        assert len(rows) == 4

    def test_filter_by_provider(self, db, svc):
        _seed_catalog(db)
        rows, total = svc.list_catalog(db, provider_key="anthropic")
        assert total == 1
        assert rows[0].provider_key == "anthropic"

    def test_filter_by_mode(self, db, svc):
        _seed_catalog(db)
        rows, total = svc.list_catalog(db, mode="embedding")
        assert total == 1
        assert rows[0].model_name == "text-embedding-3-small"

    def test_filter_by_recommended(self, db, svc):
        _seed_catalog(db)
        rows, total = svc.list_catalog(db, recommended=True)
        assert total == 2
        assert all(r.recommended for r in rows)

    def test_q_matches_display_and_model_name(self, db, svc):
        _seed_catalog(db)
        rows, total = svc.list_catalog(db, q="sonnet")
        assert total == 1
        rows, total = svc.list_catalog(db, q="gpt-4o")  # model_name
        assert total == 1

    def test_bad_mode_raises(self, db, svc):
        _seed_catalog(db)
        with pytest.raises(LlmCatalogError) as ei:
            svc.list_catalog(db, mode="nope")
        assert ei.value.code == "invalid_value"

    def test_pagination(self, db, svc):
        _seed_catalog(db)
        page1, total = svc.list_catalog(db, limit=2, offset=0)
        page2, _ = svc.list_catalog(db, limit=2, offset=2)
        assert total == 4
        assert len(page1) == 2 and len(page2) == 2


class TestGet:
    def test_get_ok(self, db, svc):
        _seed_catalog(db)
        e = svc.get_entry(db, "llmcat-openai-gpt-4o")
        assert e.model_name == "gpt-4o"

    def test_get_missing_404(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.get_entry(db, "llmcat-missing")
        assert ei.value.code == "not_found"


class TestImport:
    def test_import_creates_configured_instance(self, db, svc):
        _seed_catalog(db)
        model = svc.import_to_model(db, "t1", "llmcat-openai-gpt-4o")
        db.commit()

        assert model.id.startswith("llm-")
        assert model.tenant_id == "t1"
        assert model.catalog_id == "llmcat-openai-gpt-4o"
        assert model.enabled is True
        assert model.model_name == "gpt-4o"
        assert model.name == "GPT-4o"
        assert model.mode == "chat"
        # copied descriptive defaults
        assert model.cost_per_input_token == 0.0025
        assert model.cost_per_output_token == 0.01
        # capability flags surfaced as boolean columns
        assert model.supports_vision is True
        assert model.supports_function_calling is True
        # provider resolved + lazy-created
        prov = db.get(LlmProvider, model.provider_id)
        assert prov is not None and prov.key == "openai"

    def test_import_overrides_applied(self, db, svc):
        _seed_catalog(db)
        model = svc.import_to_model(
            db, "t1", "llmcat-openai-gpt-4o",
            overrides={"name": "My GPT", "enabled": False},
        )
        db.commit()
        assert model.name == "My GPT"
        assert model.enabled is False

    def test_import_reuses_existing_provider(self, db, svc):
        _seed_catalog(db)
        db.add(LlmProvider(id="prov-existing", tenant_id="t1", key="openai",
                           display_name="OpenAI"))
        db.commit()
        model = svc.import_to_model(db, "t1", "llmcat-openai-gpt-4o")
        db.commit()
        assert model.provider_id == "prov-existing"

    def test_import_copies_media_defaults(self, db, svc):
        """Media whitelists / per-unit cost / drop-params / intrinsic
        facts on the catalog entry are copied onto the imported instance."""
        _seed_catalog(db)
        model = svc.import_to_model(db, "t1", "llmcat-openai-dall-e-3")
        db.commit()
        assert model.mode == "image_generation"
        assert model.cost_per_image == 0.04
        assert model.supported_image_sizes == ["1024x1024", "1024x1792"]
        assert model.supported_image_qualities == ["standard", "hd"]
        assert model.max_images_per_request == 1
        assert model.additional_drop_params == ["top_p"]
        assert model.parameters == 12.0
        # template + catalog tags merge — catalog tags must be present
        assert "image" in (model.tags or [])

    def test_import_missing_entry_404(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.import_to_model(db, "t1", "llmcat-nope")
        assert ei.value.code == "not_found"

    def test_import_is_tenant_scoped(self, db, svc):
        _seed_catalog(db)
        m1 = svc.import_to_model(db, "t1", "llmcat-openai-gpt-4o")
        db.commit()
        # The created instance + its lazy provider belong to t1 only.
        assert m1.tenant_id == "t1"
        t2_models = db.query(LLModel).filter(LLModel.tenant_id == "t2").all()
        assert t2_models == []


class TestCreate:
    def test_create_ok(self, db, svc):
        entry = svc.create_entry(
            db,
            provider_key="OpenAI",  # normalised to lowercase
            model_name="gpt-5",
            display_name="GPT-5",
            mode="chat",
            description="next gen",
            context_window=200000,
            max_output_tokens=8192,
            default_cost_per_input_token=0.001,
            default_cost_per_output_token=0.002,
            icon_url="https://x/o.svg",
            recommended=True,
            capabilities={"supports_vision": True},
        )
        db.commit()
        assert entry.id.startswith("llmcat-")
        assert entry.source == "custom"
        assert entry.provider_key == "openai"
        assert entry.model_name == "gpt-5"
        assert entry.recommended is True
        assert entry.deprecated is False
        assert entry.capabilities == {"supports_vision": True}

    def test_create_duplicate_409(self, db, svc):
        _seed_catalog(db)
        with pytest.raises(LlmCatalogError) as ei:
            svc.create_entry(
                db, provider_key="openai", model_name="gpt-4o",
                display_name="dup", mode="chat",
            )
        assert ei.value.code == "duplicate"

    def test_create_bad_mode_400(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.create_entry(
                db, provider_key="openai", model_name="gpt-x",
                display_name="x", mode="bogus",
            )
        assert ei.value.code == "invalid_value"

    def test_create_missing_provider_key(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.create_entry(
                db, provider_key="  ", model_name="m",
                display_name="x", mode="chat",
            )
        assert ei.value.code == "invalid_value"


class TestUpdate:
    def test_update_partial(self, db, svc):
        _seed_catalog(db)
        entry = svc.update_entry(
            db, "llmcat-openai-gpt-4o",
            display_name="GPT-4o (renamed)", deprecated=True,
        )
        db.commit()
        assert entry.display_name == "GPT-4o (renamed)"
        assert entry.deprecated is True
        # untouched fields preserved
        assert entry.recommended is True
        assert entry.model_name == "gpt-4o"

    def test_update_only_sets_provided(self, db, svc):
        _seed_catalog(db)
        before = svc.get_entry(db, "llmcat-openai-gpt-4o")
        original_desc = before.description
        entry = svc.update_entry(db, "llmcat-openai-gpt-4o", recommended=False)
        db.commit()
        assert entry.recommended is False
        assert entry.description == original_desc

    def test_update_bad_mode_400(self, db, svc):
        _seed_catalog(db)
        with pytest.raises(LlmCatalogError) as ei:
            svc.update_entry(db, "llmcat-openai-gpt-4o", mode="bogus")
        assert ei.value.code == "invalid_value"

    def test_update_missing_404(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.update_entry(db, "llmcat-nope", display_name="x")
        assert ei.value.code == "not_found"


class TestDelete:
    def test_delete_ok(self, db, svc):
        _seed_catalog(db)
        svc.delete_entry(db, "llmcat-openai-gpt-4o")
        db.commit()
        with pytest.raises(LlmCatalogError) as ei:
            svc.get_entry(db, "llmcat-openai-gpt-4o")
        assert ei.value.code == "not_found"

    def test_delete_missing_404(self, db, svc):
        with pytest.raises(LlmCatalogError) as ei:
            svc.delete_entry(db, "llmcat-nope")
        assert ei.value.code == "not_found"
