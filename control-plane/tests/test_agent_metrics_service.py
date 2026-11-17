# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
AgentMetricsService — per-agent runtime-authz activity over proxy_logs. Seeds
rows on SQLite and asserts the outcome distribution, top-agent rollups (deny /
approval rates), and the denied-action feed.
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
from app.database.models.proxy_log import ProxyLog
from app.services.agent_metrics_service import AgentMetricsService

TENANT = "t-agent-metrics"
REQUIRED = [ProxyLog.__table__]


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
        proxy_type="mcp",
        request_id=str(uuid.uuid4()),
        end_user_session_id="sess-1",
        http_method="POST",
        endpoint="/v1/proxy/mcp/x",
        success=True,
    )
    base.update(kw)
    return ProxyLog(**base)


@pytest.fixture()
def svc():
    return AgentMetricsService()


def test_outcome_distribution_and_top_agents(db, svc):
    db.add_all([
        # agent-a: 2 allow, 1 deny, 1 approval
        _plog(actor_agent_id="agent-a", agent_authz_decision="allow"),
        _plog(actor_agent_id="agent-a", agent_authz_decision="allow"),
        _plog(actor_agent_id="agent-a", agent_authz_decision="deny",
              mcp_capability_name="shell.exec", agent_grant_id="g1"),
        _plog(actor_agent_id="agent-a", agent_authz_decision="approval_required"),
        # agent-b: 1 allow
        _plog(actor_agent_id="agent-b", agent_authz_decision="allow"),
        # non-agent row must be excluded
        _plog(actor_agent_id=None, agent_authz_decision=None),
    ])
    db.commit()

    out = svc.activity(db, tenant_id=TENANT, period_hours=0, top_n=10)
    assert out["total_actions"] == 5, "only agent-driven rows counted"
    assert out["allowed"] == 3
    assert out["denied"] == 1
    assert out["approval_required"] == 1
    assert out["deny_rate"] == 20.0
    assert out["approval_rate"] == 20.0

    # Top agents ordered by volume; agent-a first with its rates.
    top = {a["agent_id"]: a for a in out["top_agents"]}
    assert top["agent-a"]["calls"] == 4
    assert top["agent-a"]["denied"] == 1
    assert top["agent-a"]["approval_required"] == 1
    assert top["agent-a"]["deny_rate"] == 25.0
    assert top["agent-b"]["calls"] == 1
    assert out["top_agents"][0]["agent_id"] == "agent-a"

    # Denied feed carries the capability + grant for drill-down.
    assert len(out["recent_denied"]) == 1
    assert out["recent_denied"][0]["agent_id"] == "agent-a"
    assert out["recent_denied"][0]["capability"] == "shell.exec"
    assert out["recent_denied"][0]["grant_id"] == "g1"


def test_allowed_counts_ungated_actions(db, svc):
    # Regression (Mission Control "0 allowed" bug): most governed agent actions
    # run WITHOUT an explicit ALLOW decision — only denials and approval-gates
    # are stamped, so ungated actions carry a NULL decision. "Allowed" is the
    # complement (ran = total - denied - approval), not an `== ALLOW` equality
    # count, which read 0 while nearly everything was in fact allowed.
    db.add_all([
        _plog(actor_agent_id="agent-a", agent_authz_decision=None),   # ungated → ran
        _plog(actor_agent_id="agent-a", agent_authz_decision=None),
        _plog(actor_agent_id="agent-a", agent_authz_decision=None),
        _plog(actor_agent_id="agent-a", agent_authz_decision="deny", agent_grant_id="g1"),
        _plog(actor_agent_id="agent-a", agent_authz_decision="approval_required"),
    ])
    db.commit()

    out = svc.activity(db, tenant_id=TENANT, period_hours=0, top_n=10)
    assert out["total_actions"] == 5
    assert out["denied"] == 1
    assert out["approval_required"] == 1
    assert out["allowed"] == 3, "the 3 ungated actions ran; allowed must not read 0"


def test_activity_group_filter_scopes_to_group(db, svc):
    # Design Note 002 global filter — scoping to a Resource Group (an AI System is an
    # RG with group_type='ai_system') counts only that RG's agent rows.
    db.add_all([
        _plog(actor_agent_id="agent-a", agent_authz_decision="allow", resource_group_id="rg-x"),
        _plog(actor_agent_id="agent-a", agent_authz_decision="deny", resource_group_id="rg-x"),
        _plog(actor_agent_id="agent-b", agent_authz_decision="allow", resource_group_id="rg-y"),
        _plog(actor_agent_id="agent-c", agent_authz_decision="allow"),  # no RG
    ])
    db.commit()

    out = svc.activity(db, tenant_id=TENANT, period_hours=0, top_n=10, group_id="rg-x")
    assert out["total_actions"] == 2
    assert out["allowed"] == 1
    assert out["denied"] == 1
    assert {a["agent_id"] for a in out["top_agents"]} == {"agent-a"}


def test_empty_when_no_agent_traffic(db, svc):
    db.add(_plog(actor_agent_id=None))
    db.commit()
    out = svc.activity(db, tenant_id=TENANT, period_hours=0, top_n=10)
    assert out["total_actions"] == 0
    assert out["top_agents"] == []
    assert out["recent_denied"] == []
    assert out["deny_rate"] == 0.0


def test_semantic_policy_shadow_and_block_surfaced(db, svc):
    db.add_all([
        _plog(proxy_subtype="semantic_policy_shadow", semantic_policy_id="sgp-1",
              error_message="PII to peer", actor_agent_id="agent-a"),
        _plog(proxy_subtype="semantic_policy_shadow", semantic_policy_id="sgp-1",
              error_message="PII to peer"),
        _plog(proxy_subtype="semantic_policy_block", semantic_policy_id="sgp-2"),
    ])
    db.commit()

    out = svc.activity(db, tenant_id=TENANT, period_hours=0, top_n=10)
    sgp = out["semantic_policy"]
    assert sgp["blocked"] == 1
    assert sgp["shadow_would_block"] == 2
    assert len(sgp["recent_shadow"]) == 2
    assert sgp["recent_shadow"][0]["policy_id"] == "sgp-1"
