# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AssetRegistryService tests

In-memory SQLite harness. Covers:
  * sync_tenant discovers one row per seeded config asset across all 6 kinds
  * re-sync refreshes display_name but PRESERVES operator-set governance
  * orphan flagged when a source row is deleted
  * create / get / update / delete
  * tenant isolation
  * list filters (asset_kind / status) + pagination + q
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
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill
from app.database.models.ai_asset import AiAsset
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import ResourceGroup
from app.database.models.agent_identity import AgentIdentity
from app.database.models.skill_source import SkillSource
from app.database.models.tenant import Tenant
from app.services.asset_registry_service import (
    AssetRegistryError,
    AssetRegistryService,
)


REQUIRED_TABLES = [
    Tenant.__table__,
    LlmProviderCatalog.__table__,  # llm_providers.catalog_id FKs here
    LlmProvider.__table__,
    LlmCatalog.__table__,
    LLModel.__table__,
    LLMRoutingGroup.__table__,
    MCPServer.__table__,
    SkillSource.__table__,   # agent_skills.source_repo_id FKs here
    AgentSkill.__table__,
    AgentCard.__table__,
    # AI System discovery reads resource_groups (group_type='ai_system') and
    # agent identities — the registry now surfaces both as first-class assets.
    ResourceGroup.__table__,
    AgentIdentity.__table__,
    AiAsset.__table__,
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
    def _enable_sqlite_fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    # Two tenants for isolation tests.
    for tid in ("t1", "t2"):
        s.add(Tenant(
            id=tid, name=tid, display_name=tid.upper(),
            primary_contact_email=f"a@{tid}.example",
        ))
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def svc():
    return AssetRegistryService(BrutorLogger("test-asset-svc"))


def _seed_all_kinds(s, tenant_id="t1"):
    """Seed exactly one source row per discoverable kind. Returns a dict
    of {asset_kind: ref_id}."""
    prov = LlmProvider(id="prov-1", tenant_id=tenant_id, key="openai",
                       display_name="OpenAI Provider")
    s.add(prov)
    s.flush()
    model = LLModel(id="model-1", tenant_id=tenant_id, provider_id="prov-1",
                    name="GPT-4o", model_name="gpt-4o")
    rg = LLMRoutingGroup(id="rg-1", tenant_id=tenant_id, name="Fast Pool")
    mcp = MCPServer(id="mcp-1", tenant_id=tenant_id, name="Files MCP",
                    base_url="https://mcp.example")
    skill = AgentSkill(id="skill-1", tenant_id=tenant_id, name="summarise",
                       description="d", skill_md_content="# x",
                       skill_md_hash="h")
    card = AgentCard(id="card-1", tenant_id=tenant_id, name="Billing Agent",
                     card_json={"name": "billing", "capabilities": []})
    s.add_all([model, rg, mcp, skill, card])
    s.commit()
    return {
        "llm_provider": "prov-1",
        "llm_model": "model-1",
        "routing_group": "rg-1",
        "mcp_server": "mcp-1",
        "skill": "skill-1",
        "agent_card": "card-1",
    }


# =============================================================================
# Sync / discovery
# =============================================================================


class TestSync:
    def test_sync_creates_one_row_per_kind(self, db, svc):
        seeded = _seed_all_kinds(db)
        result = svc.sync_tenant(db, "t1")
        db.commit()

        assert result["created"] == 6
        assert result["orphaned"] == 0
        assert result["total"] == 6

        rows, total = svc.list_assets(db, "t1")
        assert total == 6
        by_kind = {r.asset_kind: r for r in rows}
        assert set(by_kind) == set(seeded)
        for kind, ref in seeded.items():
            r = by_kind[kind]
            assert r.ref_id == ref
            assert r.source == "discovered"
            assert r.lifecycle_stage == "active"
            assert r.status == "ok"
            assert r.display_name  # seeded from the source display column

    def test_resync_is_idempotent_no_new_rows(self, db, svc):
        _seed_all_kinds(db)
        svc.sync_tenant(db, "t1")
        db.commit()
        result = svc.sync_tenant(db, "t1")
        db.commit()
        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["total"] == 6

    def test_resync_refreshes_display_name_only(self, db, svc):
        _seed_all_kinds(db)
        svc.sync_tenant(db, "t1")
        db.commit()

        # Operator sets governance on the model asset.
        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        asset = rows[0]
        svc.update_asset(db, "t1", asset.id, owner="alice",
                         lifecycle_stage="deprecated",
                         intended_use="internal", enforcement_mode="enforce",
                         gpai_classification="gpai",
                         tags=["pii"], status="review_due")
        db.commit()

        # Upstream display name changes.
        model = db.get(LLModel, "model-1")
        model.name = "GPT-4o (renamed)"
        db.commit()

        result = svc.sync_tenant(db, "t1")
        db.commit()
        assert result["updated"] == 1

        refreshed = svc.get_asset(db, "t1", asset.id)
        # display_name refreshed...
        assert refreshed.display_name == "GPT-4o (renamed)"
        # ...governance fields preserved.
        assert refreshed.owner == "alice"
        assert refreshed.lifecycle_stage == "deprecated"
        assert refreshed.gpai_classification == "gpai"
        assert refreshed.intended_use == "internal"
        assert refreshed.enforcement_mode == "enforce"
        assert refreshed.tags == ["pii"]
        assert refreshed.status == "review_due"

    def test_orphan_flagged_when_source_deleted(self, db, svc):
        _seed_all_kinds(db)
        svc.sync_tenant(db, "t1")
        db.commit()

        # Delete the MCP server config row. Use a core DELETE rather than
        # ORM delete so we don't trigger MCPServer's relationship cascades
        # (which would pull in a long tail of unrelated child tables).
        from sqlalchemy import delete as sa_delete
        db.execute(sa_delete(MCPServer).where(MCPServer.id == "mcp-1"))
        db.commit()

        result = svc.sync_tenant(db, "t1")
        db.commit()
        assert result["orphaned"] == 1
        assert result["total"] == 6  # orphan kept, not deleted

        rows, _ = svc.list_assets(db, "t1", asset_kind="mcp_server")
        assert rows[0].status == "flagged"

    def test_manual_asset_not_flagged_as_orphan(self, db, svc):
        # A manual asset has no source row to vanish — sync must leave it.
        svc.create_asset(db, "t1", asset_kind="llm_model",
                         ref_id="external-model", source="manual")
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1")
        manual = next(r for r in rows if r.ref_id == "external-model")
        assert manual.status == "ok"
        assert manual.source == "manual"


# =============================================================================
# CRUD
# =============================================================================


class TestCrud:
    def test_create_get_update_delete(self, db, svc):
        asset = svc.create_asset(db, "t1", asset_kind="skill",
                                 ref_id="ext-skill", display_name="Ext",
                                 owner="bob")
        db.commit()
        assert asset.id.startswith("aiasset-")
        assert asset.source == "manual"
        assert asset.lifecycle_stage == "active"
        assert asset.enforcement_mode == "off"

        got = svc.get_asset(db, "t1", asset.id)
        assert got.owner == "bob"

        svc.update_asset(db, "t1", asset.id, owner="carol",
                         operated_by="third_party")
        db.commit()
        got = svc.get_asset(db, "t1", asset.id)
        assert got.owner == "carol"
        assert got.operated_by == "third_party"

        # a150_eu_ai_act_fields: a component asset cannot carry its own risk tier — Annex
        # III classifies by system purpose; the guard says so.
        with pytest.raises(AssetRegistryError) as exc:
            svc.update_asset(db, "t1", asset.id, risk_tier="limited")
        assert exc.value.code == "risk_tier_system_only"

        svc.delete_asset(db, "t1", asset.id)
        db.commit()
        with pytest.raises(AssetRegistryError) as ei:
            svc.get_asset(db, "t1", asset.id)
        assert ei.value.code == "not_found"

    def test_duplicate_create_raises(self, db, svc):
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="dup")
        db.commit()
        with pytest.raises(AssetRegistryError) as ei:
            svc.create_asset(db, "t1", asset_kind="skill", ref_id="dup")
        assert ei.value.code == "duplicate"

    def test_create_bad_vocab_raises(self, db, svc):
        with pytest.raises(AssetRegistryError) as ei:
            svc.create_asset(db, "t1", asset_kind="nope", ref_id="x")
        assert ei.value.code == "invalid_value"

    def test_update_missing_raises_404(self, db, svc):
        with pytest.raises(AssetRegistryError) as ei:
            svc.update_asset(db, "t1", "aiasset-missing", owner="x")
        assert ei.value.code == "not_found"

    def test_delete_missing_raises_404(self, db, svc):
        with pytest.raises(AssetRegistryError) as ei:
            svc.delete_asset(db, "t1", "aiasset-missing")
        assert ei.value.code == "not_found"


# =============================================================================
# Tenant isolation
# =============================================================================


class TestTenantIsolation:
    def test_get_cross_tenant_is_404(self, db, svc):
        a = svc.create_asset(db, "t1", asset_kind="skill", ref_id="s")
        db.commit()
        with pytest.raises(AssetRegistryError) as ei:
            svc.get_asset(db, "t2", a.id)
        assert ei.value.code == "not_found"

    def test_list_scoped_to_tenant(self, db, svc):
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="s1")
        svc.create_asset(db, "t2", asset_kind="skill", ref_id="s2")
        db.commit()
        rows1, total1 = svc.list_assets(db, "t1")
        rows2, total2 = svc.list_assets(db, "t2")
        assert total1 == 1 and total2 == 1
        assert rows1[0].ref_id == "s1"
        assert rows2[0].ref_id == "s2"

    def test_sync_scoped_to_tenant(self, db, svc):
        _seed_all_kinds(db, tenant_id="t1")
        svc.sync_tenant(db, "t2")
        db.commit()
        _, total2 = svc.list_assets(db, "t2")
        assert total2 == 0


# =============================================================================
# List filters + pagination + q
# =============================================================================


class TestListFilters:
    def test_filter_by_kind(self, db, svc):
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="s1")
        svc.create_asset(db, "t1", asset_kind="mcp_server", ref_id="m1")
        db.commit()
        rows, total = svc.list_assets(db, "t1", asset_kind="skill")
        assert total == 1
        assert rows[0].asset_kind == "skill"

    def test_filter_by_status(self, db, svc):
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="s1")
        a2 = svc.create_asset(db, "t1", asset_kind="skill", ref_id="s2")
        db.commit()
        svc.update_asset(db, "t1", a2.id, status="flagged")
        db.commit()
        rows, total = svc.list_assets(db, "t1", status="flagged")
        assert total == 1
        assert rows[0].id == a2.id

    def test_q_matches_display_name_and_ref_id(self, db, svc):
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="alpha-ref",
                         display_name="Alpha Skill")
        svc.create_asset(db, "t1", asset_kind="skill", ref_id="beta-ref",
                         display_name="Beta Skill")
        db.commit()
        rows, total = svc.list_assets(db, "t1", q="alpha")
        assert total == 1
        assert rows[0].ref_id == "alpha-ref"
        # match on display_name too
        rows, total = svc.list_assets(db, "t1", q="beta skill")
        assert total == 1
        assert rows[0].ref_id == "beta-ref"

    def test_pagination(self, db, svc):
        for i in range(5):
            svc.create_asset(db, "t1", asset_kind="skill", ref_id=f"s{i}")
        db.commit()
        page1, total = svc.list_assets(db, "t1", limit=2, offset=0)
        page2, _ = svc.list_assets(db, "t1", limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2 and len(page2) == 2
        assert {r.id for r in page1}.isdisjoint({r.id for r in page2})

    def test_bad_filter_value_raises(self, db, svc):
        with pytest.raises(AssetRegistryError) as ei:
            svc.list_assets(db, "t1", asset_kind="bogus")
        assert ei.value.code == "invalid_value"


# =============================================================================
# Sync resolves icon_url + source_enabled on discovered assets
# =============================================================================


class TestIconAndEnabled:
    def test_llm_model_uses_catalog_icon_and_enabled(self, db, svc):
        # Seed a catalog entry with an icon, a provider, and a model that
        # references the catalog entry and is disabled.
        db.add(LlmCatalog(
            id="llmcat-openai-gpt-4o", provider_key="openai",
            model_name="gpt-4o", display_name="GPT-4o", mode="chat",
            icon_url="https://x/catalog-icon.svg",
        ))
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="openai",
                           display_name="OpenAI", icon_url="https://x/prov.svg"))
        db.flush()
        db.add(LLModel(id="model-1", tenant_id="t1", provider_id="prov-1",
                       name="GPT-4o", model_name="gpt-4o",
                       catalog_id="llmcat-openai-gpt-4o", enabled=False))
        db.commit()

        svc.sync_tenant(db, "t1")
        db.commit()

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        asset = rows[0]
        # catalog icon wins over the provider icon
        assert asset.icon_url == "https://x/catalog-icon.svg"
        # source enabled flag mirrored
        assert asset.source_enabled is False

    def test_llm_model_falls_back_to_provider_icon(self, db, svc):
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="openai",
                           display_name="OpenAI", icon_url="https://x/prov.svg"))
        db.flush()
        db.add(LLModel(id="model-1", tenant_id="t1", provider_id="prov-1",
                       name="GPT-4o", model_name="gpt-4o", enabled=True))
        db.commit()

        svc.sync_tenant(db, "t1")
        db.commit()

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        asset = rows[0]
        assert asset.icon_url == "https://x/prov.svg"
        assert asset.source_enabled is True

    def test_llm_provider_uses_own_icon(self, db, svc):
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="openai",
                           display_name="OpenAI", icon_url="https://x/prov.svg",
                           enabled=True))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_provider")
        assert rows[0].icon_url == "https://x/prov.svg"
        assert rows[0].source_enabled is True

    def test_llm_provider_falls_back_to_blob_endpoint(self, db, svc):
        # Seeded providers carry a blob icon (from bundled SVGs) but no
        # icon_url string — the registry must advertise the control-plane
        # blob endpoint so the icon actually renders.
        prov = LlmProvider(id="prov-2", tenant_id="t1", key="anthropic",
                           display_name="Anthropic", enabled=True)
        prov.icon_data = b"<svg/>"
        prov.icon_mime_type = "image/svg+xml"
        db.add(prov)
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_provider")
        assert rows[0].icon_url == "/v1/admin/providers/prov-2/icon"

    def test_mcp_server_uses_served_blob_url_when_icon_present(self, db, svc):
        mcp = MCPServer(id="mcp-9", tenant_id="t1", name="Files MCP",
                        base_url="https://mcp.example")
        mcp.icon_data = b"<svg/>"
        mcp.icon_mime_type = "image/svg+xml"
        db.add(mcp)
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="mcp_server")
        # Control-plane icon path (public for <img>), NOT the Rust-core
        # /v1/assets path — the Asset Registry UI loads from the control
        # plane and prefixes the leading-slash path with its base URL.
        assert rows[0].icon_url == "/v1/admin/mcp-servers/mcp-9/icon"

    def test_mcp_server_without_icon_is_null(self, db, svc):
        db.add(MCPServer(id="mcp-8", tenant_id="t1", name="No Icon MCP",
                         base_url="https://mcp.example"))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="mcp_server")
        assert rows[0].icon_url is None

    def test_skill_uses_lucide_sentinel(self, db, svc):
        skill = AgentSkill(id="skill-9", tenant_id="t1", name="summarise",
                           description="d", skill_md_content="# x",
                           skill_md_hash="h")
        skill.set_metadata_value("icon_name", "BarChart3")
        db.add(skill)
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="skill")
        assert rows[0].icon_url == "lucide:BarChart3"

    def test_skill_without_icon_name_is_null(self, db, svc):
        db.add(AgentSkill(id="skill-8", tenant_id="t1", name="plain",
                          description="d", skill_md_content="# x",
                          skill_md_hash="h"))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="skill")
        assert rows[0].icon_url is None

    def test_kinds_without_icon_or_enabled_are_null(self, db, svc):
        # A routing group has no enabled flag and no icon.
        db.add(LLMRoutingGroup(id="rg-1", tenant_id="t1", name="Pool"))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()
        rows, _ = svc.list_assets(db, "t1", asset_kind="routing_group")
        assert rows[0].icon_url is None
        assert rows[0].source_enabled is None

    def test_icon_and_enabled_refreshed_on_resync(self, db, svc):
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="openai",
                           display_name="OpenAI", icon_url="https://x/a.svg",
                           enabled=True))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()

        prov = db.get(LlmProvider, "prov-1")
        prov.icon_url = "https://x/b.svg"
        prov.enabled = False
        db.commit()

        result = svc.sync_tenant(db, "t1")
        db.commit()
        assert result["updated"] == 1

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_provider")
        assert rows[0].icon_url == "https://x/b.svg"
        assert rows[0].source_enabled is False


# =============================================================================
# Observed vs Governed (Traffic Data Import provenance)
# =============================================================================


class TestObserved:
    def test_imported_disabled_model_is_observed(self, db, svc):
        # A model discovered via Traffic Data Import (source='import') that
        # hasn't been promoted is observability-only, not governed.
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="anthropic",
                           display_name="Anthropic"))
        db.flush()
        db.add(LLModel(id="m-imp", tenant_id="t1", provider_id="prov-1",
                       name="Claude (imported)", model_name="claude-haiku-4-5",
                       source="import", enabled=False))
        db.commit()

        svc.sync_tenant(db, "t1")
        db.commit()

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        asset = rows[0]
        assert asset.observed is True
        assert asset.to_dict()["observed"] is True

    def test_manual_disabled_model_is_not_observed(self, db, svc):
        # A hand-configured model that happens to be disabled is still
        # governed (operator turned it off) — NOT observed.
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="openai",
                           display_name="OpenAI"))
        db.flush()
        db.add(LLModel(id="m-man", tenant_id="t1", provider_id="prov-1",
                       name="GPT-5.5", model_name="gpt-5.5",
                       source="manual", enabled=False))
        db.commit()

        svc.sync_tenant(db, "t1")
        db.commit()

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        assert rows[0].observed is False

    def test_promoting_imported_model_clears_observed(self, db, svc):
        db.add(LlmProvider(id="prov-1", tenant_id="t1", key="anthropic",
                           display_name="Anthropic"))
        db.flush()
        db.add(LLModel(id="m-imp", tenant_id="t1", provider_id="prov-1",
                       name="Claude (imported)", model_name="claude-opus-4-6",
                       source="import", enabled=False))
        db.commit()
        svc.sync_tenant(db, "t1")
        db.commit()

        # Operator promotes the model (enables it → governed inline).
        db.get(LLModel, "m-imp").enabled = True
        db.commit()
        result = svc.sync_tenant(db, "t1")
        db.commit()
        assert result["updated"] == 1

        rows, _ = svc.list_assets(db, "t1", asset_kind="llm_model")
        assert rows[0].observed is False
