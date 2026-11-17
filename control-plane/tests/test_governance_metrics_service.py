# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
GovernanceMetricsService — guardrail/policy/PII outcomes (proxy_logs) + shadow-AI
signals (usage_logs + llm_model). Seeds rows on SQLite and asserts the counts,
the block trend/top-group rollups, and the un-approved-model detection.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.discovery import DiscoveredAsset
from app.database.models.llm_model import LLModel
from app.database.models.proxy_log import ProxyLog
from app.database.models.usage_log import UsageLog
from app.services.governance_metrics_service import GovernanceMetricsService

TENANT = "t-gov"
REQUIRED = [ProxyLog.__table__, UsageLog.__table__, LLModel.__table__,
            DiscoveredAsset.__table__]


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


def _plog(**kw) -> ProxyLog:
    base = dict(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        timestamp=datetime(2026, 6, 15, 12, tzinfo=timezone.utc),
        proxy_type="llm",
        request_id=str(uuid.uuid4()),
        end_user_session_id="sess-1",
        http_method="POST",
        endpoint="/v1/chat/completions",
        success=True,
        guardrail_blocked=False,
        argument_policy_denied=False,
        pii_detected=False,
    )
    base.update(kw)
    return ProxyLog(**base)


def _ulog(**kw) -> UsageLog:
    base = dict(
        tenant_id=TENANT,
        resource_type="llm",
        resource_id="r-1",
        request_id=str(uuid.uuid4()),
        created_at=datetime(2026, 6, 15, 12, tzinfo=timezone.utc),
        source="gateway",
        currency="USD",
    )
    base.update(kw)
    return UsageLog(**base)


@pytest.fixture()
def svc():
    return GovernanceMetricsService()


def test_guardrail_outcomes(db, svc):
    db.add_all([
        _plog(),  # clean
        _plog(guardrail_blocked=True, resource_group_id="g1"),
        _plog(guardrail_blocked=True, resource_group_id="g1"),
        _plog(argument_policy_denied=True),
        _plog(pii_detected=True),
    ])
    db.commit()
    g = svc.posture(db, tenant_id=TENANT, period_hours=0)["guardrails"]
    assert g["total_requests"] == 5
    assert g["blocked"] == 2
    assert g["policy_denied"] == 1
    assert g["pii_detected"] == 1
    assert g["block_rate"] == pytest.approx(40.0)
    assert g["top_blocked_groups"] == [{"group_id": "g1", "blocks": 2}]
    assert g["blocks_by_day"] and g["blocks_by_day"][0]["blocks"] == 2
    assert g["outcome_granularity"] == "block_only"


def test_guardrail_group_filter_scopes_to_group(db, svc):
    # Design Note 002 global filter — guardrail outcomes scope to the selected Resource
    # Group (an AI System is an RG); discovery (inventory) stays tenant-wide.
    db.add_all([
        _plog(guardrail_blocked=True, resource_group_id="rg-x"),
        _plog(guardrail_blocked=True, resource_group_id="rg-y"),
        _plog(guardrail_blocked=True, resource_group_id=None),  # no RG
    ])
    db.commit()
    g = svc.posture(db, tenant_id=TENANT, period_hours=0, group_id="rg-x")["guardrails"]
    assert g["total_requests"] == 1
    assert g["blocked"] == 1


def test_discovery_coverage_section(db, svc):
    import uuid as _uuid
    db.add_all([
        DiscoveredAsset(id=f"dasset-{_uuid.uuid4()}", tenant_id=TENANT,
                        asset_key="llm:api.openai.com:443", asset_type="llm_endpoint",
                        governance_status="governed", status="active"),
        DiscoveredAsset(id=f"dasset-{_uuid.uuid4()}", tenant_id=TENANT,
                        asset_key="llm:api.anthropic.com:443", asset_type="llm_endpoint",
                        governance_status="ungoverned", status="active",
                        computed_confidence=0.9),
        DiscoveredAsset(id=f"dasset-{_uuid.uuid4()}", tenant_id=TENANT,
                        asset_key="mcp:https://x/mcp", asset_type="mcp_server",
                        governance_status="ungoverned", status="dismissed"),
    ])
    db.commit()
    d = svc.posture(db, tenant_id=TENANT, period_hours=0)["discovery"]
    assert d["total"] == 2          # dismissed excluded
    assert d["governed"] == 1
    assert d["ungoverned"] == 1
    assert d["coverage_pct"] == pytest.approx(50.0)
    assert len(d["top_ungoverned"]) == 1
    assert d["top_ungoverned"][0]["asset_key"] == "llm:api.anthropic.com:443"


def test_shadow_ai_observed_and_unapproved(db, svc):
    # approved/curated model (enabled, has catalog_id)
    db.add(LLModel(id="m-approved", tenant_id=TENANT, name="GPT", provider_id="p",
                   model_name="gpt-5.2", enabled=True, source="manual",
                   catalog_id="cat-1"))
    # imported/disabled model = un-approved
    db.add(LLModel(id="m-shadow", tenant_id=TENANT, name="Claude", provider_id="p",
                   model_name="claude-opus-4-6", enabled=False, source="import"))
    db.add_all([
        _ulog(resource_id="m-approved", model_name="gpt-5.2",
              estimated_cost=1.0, source="gateway"),
        _ulog(resource_id="m-shadow", model_name="claude-opus-4-6",
              estimated_cost=10.0, source="import:anthropic_enterprise_analytics"),
        _ulog(resource_id="m-shadow", model_name="claude-opus-4-6",
              estimated_cost=5.0, source="import:anthropic_enterprise_analytics"),
    ])
    db.commit()
    s = svc.posture(db, tenant_id=TENANT, period_hours=0)["shadow_ai"]
    assert s["observed_requests"] == 2
    assert s["observed_cost"] == pytest.approx(15.0)
    assert s["unapproved_requests"] == 2
    assert s["unapproved_models"] == [
        {"model": "claude-opus-4-6", "cost": pytest.approx(15.0), "requests": 2}
    ]
