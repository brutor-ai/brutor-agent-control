# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the AgentCard ↔ ResourceGroup binding.

Coverage:
  * Direct binding between a card and a group roundtrips.
  * Effective-agent-cards walks ancestors with inherit_resources=True.
  * Duplicates (card bound at both parent and child) collapse to one.
  * `inherit_resources=False` breaks the chain — child sees only direct.
  * A2A limits inherit restrictively (min on numerics, bool OR for hard_stop).
  * A2A governance inherits restrictively (max-strictness access,
    union on deny classifications, intersection on allow globs).
  * Validators reject invalid shapes without falling back to 500.

Uses in-memory SQLite with FK cascades turned on, same as the other
A2A test suites.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ulid import ULID

from app.database import Base
from app.database.models.agent_card import AgentCard
from app.database.models.agent_card_capability import AgentCardCapability
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    ResourceGroup,
    _a2a_governance_restrictive_merge,
    resource_group_agent_cards,
    validate_a2a_governance,
    validate_a2a_limits,
)
from app.database.models.tenant import Tenant


REQUIRED_TABLES = [
    LlmCatalog.__table__,          # LLModel.catalog_id FKs here
    LLModel.__table__,             # Tenant.default_embedding_model_id FKs here
    Tenant.__table__,
    ResourceGroup.__table__,
    AgentCard.__table__,
    AgentCardCapability.__table__,
    resource_group_agent_cards,    # association table is a Table, not a class
    # ResourceGroup.end_user_groups selectin needs these
    # to exist even when the test never seeds an EndUserGroup row.
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)

    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _tenant(s, tid="t1"):
    t = Tenant(
        id=tid, name=tid, display_name=tid.upper(),
        organization_name=tid.upper(),
        primary_contact_email=f"a@{tid}.example",
        is_active=True, is_default=False, is_trial=False,
        suspended=False, billing_plan="self_hosted",
        features={"a2a": True},
    )
    s.add(t)
    s.flush()
    return t


def _group(s, tenant_id, name, parent=None, inherit=True, limits=None, governance=None):
    g = ResourceGroup(
        id=f"rg-{ULID()}",
        tenant_id=tenant_id,
        name=name,
        display_name=name,
        parent_group_id=parent.id if parent else None,
        hierarchy_level=(parent.hierarchy_level + 1) if parent else 0,
        hierarchy_path=f"{parent.hierarchy_path}/{name}" if parent and parent.hierarchy_path else name,
        inherit_resources=inherit,
        is_active=True,
        a2a_global_limits=limits,
        a2a_global_governance=governance,
    )
    s.add(g)
    s.flush()
    return g


def _card(s, tenant_id, name):
    c = AgentCard(
        id=f"agentcard-{ULID()}",
        tenant_id=tenant_id,
        name=name,
        version="1.0.0",
        card_json={
            "name": name,
            "description": "test",
            "version": "1.0.0",
            "capabilities": [],
        },
        trust_tier="unsigned",
        enabled=True,
    )
    s.add(c)
    s.flush()
    return c


# ─── Binding round-trip ───────────────────────────────────────────────


class TestBinding:
    def test_bind_and_query_direct(self, session):
        _tenant(session)
        g = _group(session, "t1", "root")
        c = _card(session, "t1", "billing-agent")
        g.agent_cards.append(c)
        session.commit()

        session.refresh(g)
        direct = list(g.agent_cards)
        assert [d.id for d in direct] == [c.id]
        # Back-reference
        session.refresh(c)
        assert g.id in [gg.id for gg in c.resource_groups]

    def test_unbind_removes_association(self, session):
        _tenant(session)
        g = _group(session, "t1", "root")
        c = _card(session, "t1", "x")
        g.agent_cards.append(c)
        session.commit()

        g.agent_cards.remove(c)
        session.commit()
        assert list(g.agent_cards) == []
        assert list(c.resource_groups) == []


# ─── Effective agent cards with inheritance ────────────────────────────


class TestEffectiveAgentCards:
    def test_child_inherits_parent_cards(self, session):
        _tenant(session)
        parent = _group(session, "t1", "parent")
        child = _group(session, "t1", "child", parent=parent)
        c = _card(session, "t1", "shared-card")
        parent.agent_cards.append(c)
        session.commit()

        effective = child.get_effective_agent_cards()
        assert [e.id for e in effective] == [c.id]

    def test_direct_and_inherited_dedup(self, session):
        _tenant(session)
        parent = _group(session, "t1", "parent")
        child = _group(session, "t1", "child", parent=parent)
        c = _card(session, "t1", "shared")
        # Same card at both levels — effective should still have one entry.
        parent.agent_cards.append(c)
        child.agent_cards.append(c)
        session.commit()

        effective = child.get_effective_agent_cards()
        assert [e.id for e in effective] == [c.id]

    def test_inherit_resources_false_blocks_inheritance(self, session):
        _tenant(session)
        parent = _group(session, "t1", "parent")
        child = _group(session, "t1", "child", parent=parent, inherit=False)
        c = _card(session, "t1", "parent-only")
        parent.agent_cards.append(c)
        session.commit()

        # Parent still sees its card.
        assert [e.id for e in parent.get_effective_agent_cards()] == [c.id]
        # Child with inherit=False should see nothing direct, nothing inherited.
        assert child.get_effective_agent_cards() == []


# ─── A2A limits inheritance (restrictive merge) ────────────────────────


class TestA2ALimitsInheritance:
    def test_child_tightens_numeric(self, session):
        _tenant(session)
        parent = _group(
            session, "t1", "parent",
            limits={"invocations": {"daily_limit": 1000}},
        )
        child = _group(
            session, "t1", "child", parent=parent,
            limits={"invocations": {"daily_limit": 100}},
        )
        session.commit()

        eff = child.get_effective_a2a_limits()
        assert eff["invocations"]["daily_limit"] == 100

    def test_child_cannot_relax_parent_numeric(self, session):
        _tenant(session)
        parent = _group(
            session, "t1", "parent",
            limits={"invocations": {"daily_limit": 100}},
        )
        child = _group(
            session, "t1", "child", parent=parent,
            limits={"invocations": {"daily_limit": 10_000}},
        )
        session.commit()

        eff = child.get_effective_a2a_limits()
        assert eff["invocations"]["daily_limit"] == 100

    def test_hard_stop_true_wins(self, session):
        _tenant(session)
        parent = _group(
            session, "t1", "parent",
            limits={"invocations": {"hard_stop": False}},
        )
        child = _group(
            session, "t1", "child", parent=parent,
            limits={"invocations": {"hard_stop": True}},
        )
        session.commit()

        eff = child.get_effective_a2a_limits()
        assert eff["invocations"]["hard_stop"] is True

    def test_chain_depth_tightens(self, session):
        _tenant(session)
        parent = _group(
            session, "t1", "parent",
            limits={"chain": {"max_delegation_depth": 5}},
        )
        child = _group(
            session, "t1", "child", parent=parent,
            limits={"chain": {"max_delegation_depth": 2}},
        )
        session.commit()
        eff = child.get_effective_a2a_limits()
        assert eff["chain"]["max_delegation_depth"] == 2


# ─── A2A governance merge ──────────────────────────────────────────────


class TestA2AGovernanceMerge:
    def test_default_access_max_strictness(self):
        base = {"capability": {"default_access": "enabled"}}
        _a2a_governance_restrictive_merge(
            base, {"capability": {"default_access": "approval_required"}}
        )
        assert base["capability"]["default_access"] == "approval_required"

    def test_default_access_does_not_relax(self):
        base = {"capability": {"default_access": "disabled"}}
        _a2a_governance_restrictive_merge(
            base, {"capability": {"default_access": "enabled"}}
        )
        assert base["capability"]["default_access"] == "disabled"

    def test_deny_classifications_union(self):
        base = {"data": {"deny_classifications": ["PII"]}}
        _a2a_governance_restrictive_merge(
            base, {"data": {"deny_classifications": ["PHI"]}}
        )
        assert sorted(base["data"]["deny_classifications"]) == ["PHI", "PII"]

    @pytest.mark.parametrize(
        "parent, child",
        [
            (["https://a/*", "https://b/*"], ["https://b/*", "https://c/*"]),
            (["https://partner/*"], []),
            ([], ["https://partner/*"]),
        ],
    )
    def test_allow_lists_are_not_merged_into_one(self, parent, child):
        # The gateway judges an address by each level's list on its own:
        # no single list says "matches both", so the merged document
        # carries none (the per-level lists are shown instead).
        base = {"remote": {"allowed_globs": parent}, "data": {"deny_classifications": ["PII"]}}
        _a2a_governance_restrictive_merge(base, {"remote": {"allowed_globs": child}})
        assert "remote" not in base
        assert base["data"] == {"deny_classifications": ["PII"]}


class TestA2AGovernanceInheritance:
    def test_effective_merges_ancestors(self, session):
        _tenant(session)
        p = _group(
            session, "t1", "p",
            governance={
                "capability": {"default_access": "approval_required"},
                "data": {"deny_classifications": ["PII"]},
            },
        )
        c = _group(
            session, "t1", "c", parent=p,
            governance={
                "data": {"deny_classifications": ["PHI"]},
            },
        )
        session.commit()

        eff = c.get_effective_a2a_governance()
        assert eff["capability"]["default_access"] == "approval_required"
        assert sorted(eff["data"]["deny_classifications"]) == ["PHI", "PII"]


# ─── Validators ────────────────────────────────────────────────────────


class TestValidators:
    def test_limits_reject_negative_daily_limit(self):
        ok, _err = validate_a2a_limits({"invocations": {"daily_limit": -1}})
        assert not ok

    def test_limits_reject_warning_over_100(self):
        ok, _err = validate_a2a_limits(
            {"invocations": {"daily_warning_percent": 150}}
        )
        assert not ok

    def test_limits_reject_depth_over_absolute_cap(self):
        ok, _err = validate_a2a_limits({"chain": {"max_delegation_depth": 99}})
        assert not ok

    def test_governance_reject_bad_access(self):
        ok, _err = validate_a2a_governance(
            {"capability": {"default_access": "bogus"}}
        )
        assert not ok

    def test_governance_reject_unknown_section(self):
        ok, _err = validate_a2a_governance({"new_section": {}})
        assert not ok

    def test_governance_accept_valid(self):
        ok, err = validate_a2a_governance(
            {
                "capability": {
                    "default_access": "approval_required",
                    "approval_timeout_seconds": 300,
                },
                "data": {"deny_classifications": ["PII"]},
                "remote": {"allowed_globs": ["https://ok/*"]},
            }
        )
        assert ok, err
