# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Factsheet service tests

In-memory SQLite harness. Seeds an ai_assets row + the source config
rows + proxy_logs rows and asserts:

  * per-kind proxy_logs join correctness (llm_model via server_id; skill
    via skill_id; provider / routing_group via member models; mcp / a2a
    proxy_type filters),
  * usage aggregation incl. derived total-tokens (with a NULL-completion
    row),
  * guardrail / compliance / residency counts,
  * access-scope from resource-group bindings,
  * empty-evidence zeroing,
  * window filtering,
  * snapshot capture / list / get.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import (
    AgentSkill,
    resource_group_agent_skills,
)
from app.database.models.ai_asset import AiAsset
from app.database.models.ai_asset_factsheet import AiAssetFactsheet
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.skill_source import SkillSource
from app.database.models.tenant import Tenant
from app.database.models.end_user import EndUser
from app.database.models.usage_log import UsageLog
from app.database.models.virtual_server import VirtualServer
from app.services.factsheet_service import FactsheetError, FactsheetService


REQUIRED_TABLES = [
    LlmCatalog.__table__,   # LLModel.catalog_id FKs into it
    LLModel.__table__,
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LLMRoutingGroup.__table__,
    MCPServer.__table__,
    VirtualServer.__table__,
    OAuthAppClient.__table__,
    ServerConfig.__table__,
    SkillSource.__table__,
    AgentSkill.__table__,
    AgentCard.__table__,
    AiAsset.__table__,
    AiAssetFactsheet.__table__,
    ProxyLog.__table__,
    EndUser.__table__,   # usage_logs.user_id FK parent (SQLite checks even NULL FKs)
    UsageLog.__table__,
    ResourceGroup.__table__,
    resource_group_llm_models,
    resource_group_server_configs,
    resource_group_agent_skills,
    resource_group_agent_cards,
    Tenant.__table__,
]

TENANT = "t1"
OTHER = "t2"


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
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    for tid in (TENANT, OTHER):
        s.add(Tenant(
            id=tid, name=tid, display_name=tid, organization_name=tid,
            primary_contact_email=f"a@{tid}.x", is_active=True,
            is_default=False, is_trial=False, suspended=False,
            billing_plan="self_hosted",
        ))
    s.commit()
    _PROVIDER_SEEDED.clear()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _log(
    *, tenant_id=TENANT, proxy_type="llm", server_id=None, skill_id=None,
    prompt=None, completion=None, cost=None, success=True, duration=None,
    when=None, guardrail_blocked=False, pii_detected=False,
    argument_policy_denied=False, region=None, residency_class=None,
    compliance_tags=None,
) -> ProxyLog:
    return ProxyLog(
        id=f"plog-{uuid.uuid4().hex}",
        tenant_id=tenant_id,
        timestamp=when or _now(),
        proxy_type=proxy_type,
        server_id=server_id,
        skill_id=skill_id,
        end_user_session_id=f"sess-{uuid.uuid4().hex[:8]}",
        http_method="POST",
        endpoint="/v1/proxy/x",
        success=success,
        llm_prompt_tokens=prompt,
        llm_completion_tokens=completion,
        llm_estimated_cost_usd=cost,
        duration_ms=duration,
        guardrail_blocked=guardrail_blocked,
        pii_detected=pii_detected,
        argument_policy_denied=argument_policy_denied,
        region_routed_to=region,
        residency_class=residency_class,
        compliance_tags=compliance_tags,
    )


_PROVIDER_SEEDED: set = set()


def _model(db, model_id, *, tenant_id=TENANT, provider_id=None,
           routing_group_id=None, name="M", model_name="m", enabled=True):
    """Add an LLModel, ensuring a default provider exists (provider_id is
    NOT NULL on llm_model)."""
    if provider_id is None:
        provider_id = f"prov-default-{tenant_id}"
        if (tenant_id, provider_id) not in _PROVIDER_SEEDED:
            db.add(LlmProvider(id=provider_id, tenant_id=tenant_id,
                               key=f"k-{provider_id}", display_name="Default"))
            db.flush()
            _PROVIDER_SEEDED.add((tenant_id, provider_id))
    db.add(LLModel(id=model_id, tenant_id=tenant_id, provider_id=provider_id,
                   name=name, model_name=model_name, enabled=enabled,
                   routing_group_id=routing_group_id))


def _asset(db, kind, ref_id, tenant_id=TENANT) -> AiAsset:
    a = AiAsset(
        id=f"aiasset-{uuid.uuid4().hex}",
        tenant_id=tenant_id, asset_kind=kind, ref_id=ref_id,
        display_name=f"{kind}:{ref_id}",
    )
    db.add(a)
    db.flush()
    return a


def svc():
    return FactsheetService(BrutorLogger("test-factsheet"))


# ===========================================================================
# Per-kind join correctness + usage aggregation
# ===========================================================================


class TestLlmModelJoin:
    def test_usage_and_derived_total_tokens(self, db):
        db.add(LlmProvider(id="prov-1", tenant_id=TENANT, key="openai",
                           display_name="OpenAI"))
        db.add(LLModel(id="model-1", tenant_id=TENANT, provider_id="prov-1",
                       name="GPT-4o", model_name="gpt-4o", enabled=True))
        # 3 rows for our model: one normal, one with NULL completion, one error.
        db.add(_log(server_id="model-1", prompt=100, completion=50, cost=0.10,
                    duration=200.0, success=True))
        db.add(_log(server_id="model-1", prompt=80, completion=None, cost=0.05,
                    duration=100.0, success=True))
        db.add(_log(server_id="model-1", prompt=10, completion=5, cost=0.01,
                    duration=300.0, success=False))
        # A row for a DIFFERENT model — must NOT be counted.
        db.add(_log(server_id="model-zzz", prompt=999, completion=999, cost=9.9))
        # A non-LLM row on the same server_id — must NOT be counted.
        db.add(_log(server_id="model-1", proxy_type="mcp", prompt=1, completion=1))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        u = fs["usage"]
        assert u["requests"] == 3
        assert u["prompt_tokens"] == 190        # 100+80+10
        assert u["completion_tokens"] == 55     # 50+0+5
        # Derived total = prompt + completion (NULL completion → +0).
        assert u["total_tokens"] == 245
        assert round(u["estimated_cost_usd"], 2) == 0.16
        assert u["success_count"] == 2
        assert u["error_count"] == 1
        assert u["error_rate"] == round(1 / 3, 4)
        assert u["avg_duration_ms"] == 200.0
        assert u["first_seen"] is not None
        assert u["last_seen"] is not None
        # config section reflects the source model.
        assert fs["config"]["model_name"] == "gpt-4o"
        assert fs["config"]["enabled"] is True


class TestSkillJoin:
    def test_skill_uses_skill_id_column(self, db):
        db.add(AgentSkill(id="skill-1", tenant_id=TENANT, name="summarise",
                          description="d", skill_md_content="# x",
                          skill_md_hash="h"))
        db.add(_log(proxy_type="mcp", skill_id="skill-1", success=True))
        db.add(_log(proxy_type="mcp", skill_id="skill-1", success=False))
        db.add(_log(proxy_type="mcp", skill_id="other-skill"))
        a = _asset(db, "skill", "skill-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 2
        assert fs["usage"]["error_count"] == 1
        assert fs["config"]["name"] == "summarise"


class TestMcpJoin:
    def test_mcp_includes_virtual_mcp(self, db):
        db.add(MCPServer(id="mcp-1", tenant_id=TENANT, name="Files",
                         base_url="https://mcp.example"))
        db.add(_log(proxy_type="mcp", server_id="mcp-1"))
        db.add(_log(proxy_type="virtual-mcp", server_id="mcp-1"))
        db.add(_log(proxy_type="llm", server_id="mcp-1"))  # excluded
        a = _asset(db, "mcp_server", "mcp-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 2
        assert fs["config"]["base_url"] == "https://mcp.example"


class TestA2aJoin:
    def test_agent_card_proxy_type_a2a(self, db):
        db.add(AgentCard(id="card-1", tenant_id=TENANT, name="Billing",
                         card_json={"name": "b", "capabilities": []}))
        db.add(_log(proxy_type="a2a", server_id="card-1"))
        db.add(_log(proxy_type="a2a", server_id="card-1"))
        db.add(_log(proxy_type="llm", server_id="card-1"))  # excluded
        a = _asset(db, "agent_card", "card-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 2
        assert fs["config"]["name"] == "Billing"


class TestProviderJoin:
    def test_provider_member_models(self, db):
        db.add(LlmProvider(id="prov-1", tenant_id=TENANT, key="openai",
                           display_name="OpenAI"))
        db.add(LlmProvider(id="prov-other", tenant_id=TENANT, key="anthropic",
                           display_name="Anthropic"))
        db.flush()
        _model(db, "m-a", provider_id="prov-1", name="A", model_name="gpt-a")
        _model(db, "m-b", provider_id="prov-1", name="B", model_name="gpt-b")
        _model(db, "m-c", provider_id="prov-other", name="C", model_name="gpt-c")
        db.add(_log(server_id="m-a", prompt=10, completion=10))
        db.add(_log(server_id="m-b", prompt=20, completion=20))
        db.add(_log(server_id="m-c", prompt=99, completion=99))  # excluded
        a = _asset(db, "llm_provider", "prov-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 2
        assert fs["usage"]["total_tokens"] == 60
        assert fs["config"]["member_model_count"] == 2

    def test_provider_with_no_members_is_zeroed(self, db):
        db.add(LlmProvider(id="prov-empty", tenant_id=TENANT, key="x",
                           display_name="Empty"))
        db.add(_log(server_id="m-a", prompt=10, completion=10))
        a = _asset(db, "llm_provider", "prov-empty")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 0
        assert fs["usage"]["total_tokens"] == 0
        assert fs["config"]["member_model_count"] == 0


class TestRoutingGroupJoin:
    def test_routing_group_member_models(self, db):
        db.add(LLMRoutingGroup(id="rg-1", tenant_id=TENANT, name="Pool"))
        _model(db, "rm-a", routing_group_id="rg-1")
        _model(db, "rm-b", routing_group_id="rg-1")
        _model(db, "rm-c")
        db.add(_log(server_id="rm-a", prompt=5, completion=5))
        db.add(_log(server_id="rm-b", prompt=5, completion=5))
        db.add(_log(server_id="rm-c", prompt=99, completion=99))  # excluded
        a = _asset(db, "routing_group", "rg-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 2
        assert fs["usage"]["total_tokens"] == 20
        assert fs["config"]["name"] == "Pool"


# ===========================================================================
# Guardrails / compliance / residency
# ===========================================================================


class TestGovernanceSections:
    def test_guardrail_and_residency_and_compliance(self, db):
        _model(db, "model-1")
        db.add(_log(server_id="model-1", guardrail_blocked=True,
                    region="eu-west-1", residency_class="allowed",
                    compliance_tags=["soc2.cc7.2", "gdpr_art_30"]))
        db.add(_log(server_id="model-1", pii_detected=True,
                    region="us-east-1", residency_class="allowed",
                    compliance_tags=["hipaa.phi"]))
        db.add(_log(server_id="model-1", argument_policy_denied=True,
                    region="eu-west-1", residency_class="blocked",
                    compliance_tags=["soc2.cc6.6"]))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        g = fs["guardrails"]
        assert g["guardrail_blocked"] == 1
        assert g["pii_detected"] == 1
        assert g["argument_policy_denied"] == 1

        r = fs["residency"]
        assert r["regions_routed_to"] == ["eu-west-1", "us-east-1"]
        assert r["residency_class_counts"] == {"allowed": 2, "blocked": 1}

        c = fs["compliance"]["frameworks"]
        # soc2 fired on 2 rows (cc7.2 + cc6.6), counted once each.
        assert c["soc2"] == 2
        assert c["gdpr_art_30"] == 1
        assert c["hipaa"] == 1
        assert c["eu_ai_act"] == 0

    def test_compliance_row_with_two_tags_of_same_framework_counts_once(self, db):
        _model(db, "model-1")
        db.add(_log(server_id="model-1",
                    compliance_tags=["soc2.cc6.1", "soc2.cc7.2"]))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["compliance"]["frameworks"]["soc2"] == 1


# ===========================================================================
# Access scope
# ===========================================================================


class TestAccessScope:
    def test_llm_model_resource_groups(self, db):
        _model(db, "model-1")
        db.add(ResourceGroup(id="grp-1", tenant_id=TENANT, name="Engineering"))
        db.add(ResourceGroup(id="grp-2", tenant_id=TENANT, name="Sales"))
        db.flush()
        db.execute(resource_group_llm_models.insert().values(
            id=str(uuid.uuid4()), resource_group_id="grp-1",
            llm_model_id="model-1",
        ))
        db.execute(resource_group_llm_models.insert().values(
            id=str(uuid.uuid4()), resource_group_id="grp-2",
            llm_model_id="model-1",
        ))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        names = sorted(g["name"] for g in fs["access"]["resource_groups"])
        assert names == ["Engineering", "Sales"]

    def test_mcp_server_access_via_server_config(self, db):
        db.add(MCPServer(id="mcp-1", tenant_id=TENANT, name="Files",
                         base_url="https://mcp.example"))
        db.add(ServerConfig(id="cfg-1", tenant_id=TENANT, mcp_server_id="mcp-1",
                            name="Files cfg"))
        db.add(ResourceGroup(id="grp-1", tenant_id=TENANT, name="Ops"))
        db.flush()
        db.execute(resource_group_server_configs.insert().values(
            id=str(uuid.uuid4()), resource_group_id="grp-1",
            server_config_id="cfg-1",
        ))
        a = _asset(db, "mcp_server", "mcp-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert [g["name"] for g in fs["access"]["resource_groups"]] == ["Ops"]

    def test_skill_access_binding(self, db):
        db.add(AgentSkill(id="skill-1", tenant_id=TENANT, name="s",
                          description="d", skill_md_content="# x",
                          skill_md_hash="h"))
        db.add(ResourceGroup(id="grp-1", tenant_id=TENANT, name="Research"))
        db.flush()
        db.execute(resource_group_agent_skills.insert().values(
            id=str(uuid.uuid4()), resource_group_id="grp-1",
            agent_skill_id="skill-1",
        ))
        a = _asset(db, "skill", "skill-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert [g["name"] for g in fs["access"]["resource_groups"]] == ["Research"]


# ===========================================================================
# Empty evidence + window filtering + tenant scope
# ===========================================================================


class TestEdges:
    def test_empty_evidence_zeroed(self, db):
        _model(db, "model-1")
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 0
        assert fs["usage"]["total_tokens"] == 0
        assert fs["usage"]["error_rate"] == 0.0
        assert fs["guardrails"]["guardrail_blocked"] == 0
        assert fs["residency"]["regions_routed_to"] == []
        assert fs["access"]["resource_groups"] == []
        assert all(v == 0 for v in fs["compliance"]["frameworks"].values())

    def test_window_filtering_excludes_old_rows(self, db):
        _model(db, "model-1")
        old = _now() - timedelta(hours=1000)
        db.add(_log(server_id="model-1", prompt=10, completion=10, when=old))
        db.add(_log(server_id="model-1", prompt=10, completion=10, when=_now()))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        # 720h window excludes the 1000h-old row.
        fs = svc().build_factsheet(db, TENANT, a.id, period_hours=720)
        assert fs["usage"]["requests"] == 1
        assert fs["meta"]["period_hours"] == 720
        assert fs["meta"]["window_start"] is not None

        # All-time (0) includes both.
        fs_all = svc().build_factsheet(db, TENANT, a.id, period_hours=0)
        assert fs_all["usage"]["requests"] == 2
        assert fs_all["meta"]["period_hours"] == 0
        assert fs_all["meta"]["window_start"] is None

    def test_missing_asset_raises_not_found(self, db):
        with pytest.raises(FactsheetError) as e:
            svc().build_factsheet(db, TENANT, "aiasset-nope")
        assert e.value.code == "not_found"

    def test_cross_tenant_asset_not_found(self, db):
        a = _asset(db, "llm_model", "model-x", tenant_id=OTHER)
        db.commit()
        with pytest.raises(FactsheetError) as e:
            svc().build_factsheet(db, TENANT, a.id)
        assert e.value.code == "not_found"

    def test_evidence_is_tenant_scoped(self, db):
        _model(db, "model-1")
        # Same server_id, different tenant — must not leak.
        db.add(_log(server_id="model-1", prompt=10, completion=10, tenant_id=OTHER))
        db.add(_log(server_id="model-1", prompt=5, completion=5, tenant_id=TENANT))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        assert fs["usage"]["requests"] == 1
        assert fs["usage"]["total_tokens"] == 10


# ===========================================================================
# Snapshot capture / list / get + CSV flattener
# ===========================================================================


class TestSnapshots:
    def test_capture_list_get(self, db):
        _model(db, "model-1")
        db.add(_log(server_id="model-1", prompt=10, completion=10))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        s = svc()
        snap = s.capture_snapshot(db, TENANT, a.id, period_hours=720)
        db.commit()
        assert snap.id.startswith("aiasset-fs-")
        assert snap.period_hours == 720
        assert snap.snapshot_json["usage"]["total_tokens"] == 20
        assert snap.trigger == "manual"

        rows, total = s.list_snapshots(db, TENANT, a.id)
        assert total == 1
        assert rows[0].id == snap.id

        got = s.get_snapshot(db, TENANT, snap.id)
        assert got.id == snap.id

    def test_all_time_snapshot_records_zero_period(self, db):
        _model(db, "model-1")
        a = _asset(db, "llm_model", "model-1")
        db.commit()
        snap = svc().capture_snapshot(db, TENANT, a.id, period_hours=0)
        db.commit()
        assert snap.period_hours == 0

    def test_get_snapshot_cross_tenant_404(self, db):
        _model(db, "model-1")
        a = _asset(db, "llm_model", "model-1")
        db.commit()
        snap = svc().capture_snapshot(db, TENANT, a.id)
        db.commit()
        with pytest.raises(FactsheetError) as e:
            svc().get_snapshot(db, OTHER, snap.id)
        assert e.value.code == "not_found"

    def test_list_snapshots_missing_asset_404(self, db):
        with pytest.raises(FactsheetError):
            svc().list_snapshots(db, TENANT, "aiasset-nope")

    def test_csv_rows_flatten(self, db):
        _model(db, "model-1")
        db.add(_log(server_id="model-1", prompt=10, completion=10, cost=0.02,
                    region="eu-west-1", residency_class="allowed",
                    compliance_tags=["soc2.cc7.2"]))
        a = _asset(db, "llm_model", "model-1")
        db.commit()

        fs = svc().build_factsheet(db, TENANT, a.id)
        headers, rows = FactsheetService.factsheet_csv_rows(fs)
        assert "asset_id" in headers
        assert "total_tokens" in headers
        assert len(rows) == 1
        row = rows[0]
        assert row["asset_id"] == a.id
        assert row["total_tokens"] == 20
        assert row["asset_kind"] == "llm_model"
        assert "soc2" in row["compliance_frameworks"]
        assert row["regions_routed_to"] == ["eu-west-1"]


# ===========================================================================
# Observed (imported) usage — Traffic Data Import rows in usage_logs
# ===========================================================================


def _import_row(*, tenant_id=TENANT, model_id, prompt=100, completion=50,
                cost=1.5, when=None, source="import:anthropic_enterprise_analytics",
                status_code=200) -> UsageLog:
    return UsageLog(
        tenant_id=tenant_id,
        source=source,
        resource_type="llm",
        resource_id=model_id,
        resource_name="claude-x",
        model_name="claude-x",
        provider="anthropic",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=(prompt or 0) + (completion or 0),
        estimated_cost=cost,
        created_at=when or _now(),
        status_code=status_code,
        request_id=f"imp-{uuid.uuid4().hex}",
    )


class TestObservedImportedUsage:
    def test_observed_only_model_gets_imported_usage(self, db):
        """An observed model (Traffic Data Import discovery) has NO proxy_logs
        — its factsheet usage must come from import-sourced usage_logs."""
        _model(db, "model-obs", enabled=False)
        a = _asset(db, "llm_model", "model-obs")
        db.add(_import_row(model_id="model-obs", prompt=100, completion=50, cost=1.5))
        db.add(_import_row(model_id="model-obs", prompt=200, completion=100, cost=2.5))
        db.commit()

        u = svc().build_factsheet(db, TENANT, a.id)["usage"]
        assert u["requests"] == 2
        assert u["observed_requests"] == 2
        assert u["prompt_tokens"] == 300
        assert u["completion_tokens"] == 150
        assert u["total_tokens"] == 450
        assert u["estimated_cost_usd"] == 4.0
        assert u["observed_cost_usd"] == 4.0
        assert u["success_count"] == 2
        assert u["error_count"] == 0
        assert u["first_seen"] is not None and u["last_seen"] is not None

    def test_governed_and_observed_merge_without_double_count(self, db):
        """Governed traffic (proxy_logs) + imported usage merge; a gateway-
        sourced usage_logs row must NOT be counted (it is already in
        proxy_logs)."""
        _model(db, "model-mix")
        a = _asset(db, "llm_model", "model-mix")
        db.add(_log(server_id="model-mix", prompt=10, completion=5, cost=0.1))
        db.add(_import_row(model_id="model-mix", prompt=100, completion=50, cost=1.0))
        # gateway ledger row — same model, source='gateway' → excluded
        db.add(_import_row(model_id="model-mix", prompt=999, completion=999,
                           cost=99.0, source="gateway"))
        db.commit()

        u = svc().build_factsheet(db, TENANT, a.id)["usage"]
        assert u["requests"] == 2                       # 1 governed + 1 imported
        assert u["observed_requests"] == 1
        assert u["prompt_tokens"] == 110
        assert u["total_tokens"] == 165
        assert round(u["estimated_cost_usd"], 6) == 1.1
        assert u["observed_cost_usd"] == 1.0

    def test_provider_rolls_up_member_observed_usage(self, db):
        prov = "prov-obs"
        db.add(LlmProvider(id=prov, tenant_id=TENANT, key="anthropic",
                           display_name="Anthropic"))
        db.flush()
        _model(db, "model-p1", provider_id=prov)
        a = _asset(db, "llm_provider", prov)
        db.add(_import_row(model_id="model-p1", prompt=10, completion=10, cost=0.5))
        db.commit()

        u = svc().build_factsheet(db, TENANT, a.id)["usage"]
        assert u["requests"] == 1
        assert u["observed_requests"] == 1
        assert u["observed_cost_usd"] == 0.5

    def test_observed_usage_respects_window_and_tenant(self, db):
        _model(db, "model-win", enabled=False)
        a = _asset(db, "llm_model", "model-win")
        db.add(_import_row(model_id="model-win",
                           when=_now() - timedelta(days=60), cost=5.0))
        db.add(_import_row(model_id="model-win", tenant_id=OTHER, cost=7.0))
        db.commit()

        # 720h window (default) excludes the 60-day-old row; other-tenant
        # row is never visible.
        u = svc().build_factsheet(db, TENANT, a.id)["usage"]
        assert u["requests"] == 0
        assert u["observed_requests"] == 0

        # All-time window (0) picks up the old row.
        u_all = svc().build_factsheet(db, TENANT, a.id, period_hours=0)["usage"]
        assert u_all["requests"] == 1
        assert u_all["observed_cost_usd"] == 5.0

    def test_imported_error_rows_counted_as_errors(self, db):
        _model(db, "model-err", enabled=False)
        a = _asset(db, "llm_model", "model-err")
        db.add(_import_row(model_id="model-err", cost=1.0))
        db.add(_import_row(model_id="model-err", cost=0.0, status_code=429))
        db.commit()

        u = svc().build_factsheet(db, TENANT, a.id)["usage"]
        assert u["requests"] == 2
        assert u["success_count"] == 1
        assert u["error_count"] == 1
        assert u["error_rate"] == 0.5
