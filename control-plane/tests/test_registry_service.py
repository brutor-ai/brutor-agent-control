# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
RegistryService.coverage — unified governed (ai_assets) + discovered-ungoverned
inventory with "N seen, M governed" counts.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.ai_asset import AiAsset
from app.database.models.discovery import DiscoveredAsset
from app.services.registry_service import RegistryService

TENANT = "t-reg"
REQUIRED = [AiAsset.__table__, DiscoveredAsset.__table__]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_coverage_joins_governed_and_discovered(db):
    # Two governed assets (kinds with no enrichment lookup).
    db.add(AiAsset(id="a1", tenant_id=TENANT, asset_kind="llm_model", ref_id="m1",
                   display_name="GPT", source="discovered"))
    db.add(AiAsset(id="a2", tenant_id=TENANT, asset_kind="llm_provider", ref_id="p1",
                   display_name="OpenAI", source="discovered"))
    # Discovered: one ungoverned+active (counts), one governed (excluded),
    # one dismissed (excluded).
    db.add(DiscoveredAsset(id="d1", tenant_id=TENANT, asset_key="api.openai.com",
                           asset_type="llm_endpoint", governance_status="ungoverned", status="active"))
    db.add(DiscoveredAsset(id="d2", tenant_id=TENANT, asset_key="known.example",
                           asset_type="mcp_server", governance_status="governed", status="active"))
    db.add(DiscoveredAsset(id="d3", tenant_id=TENANT, asset_key="dismissed.example",
                           asset_type="llm_endpoint", governance_status="ungoverned", status="dismissed"))
    db.commit()

    out = RegistryService().coverage(db, TENANT)

    assert out["coverage"]["governed"] == 2
    assert out["coverage"]["observed"] == 0
    assert out["coverage"]["ungoverned"] == 1, "only ungoverned+active discovered assets count"
    assert out["coverage"]["total_seen"] == 3
    assert len(out["governed"]) == 2
    assert out["observed"] == []
    assert len(out["discovered_ungoverned"]) == 1
    assert out["discovered_ungoverned"][0]["asset_key"] == "api.openai.com"
    # Enrichment fields are present (None for non-agent/mcp kinds).
    for g in out["governed"]:
        assert "version" in g and "capability_count" in g


def test_coverage_splits_observed_from_governed(db):
    # An enabled/governed model, plus an import-observed one (observed=True).
    db.add(AiAsset(id="a1", tenant_id=TENANT, asset_kind="llm_model", ref_id="m1",
                   display_name="GPT-5.5", source="discovered", observed=False))
    db.add(AiAsset(id="a2", tenant_id=TENANT, asset_kind="llm_model", ref_id="m2",
                   display_name="Claude (imported)", source="discovered", observed=True))
    db.commit()

    out = RegistryService().coverage(db, TENANT)

    assert out["coverage"]["governed"] == 1, "observed imports are not counted as governed"
    assert out["coverage"]["observed"] == 1
    assert out["coverage"]["total_seen"] == 2
    assert [g["ref_id"] for g in out["governed"]] == ["m1"]
    assert [o["ref_id"] for o in out["observed"]] == ["m2"]


def test_coverage_empty_tenant(db):
    out = RegistryService().coverage(db, TENANT)
    assert out["coverage"] == {"governed": 0, "observed": 0, "ungoverned": 0, "total_seen": 0}
    assert out["governed"] == []
    assert out["observed"] == []
    assert out["discovered_ungoverned"] == []
