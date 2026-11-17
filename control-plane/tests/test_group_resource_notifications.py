# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `app.services.notifications.group_resources`.

Coverage:
  * Each member of an affected group gets exactly one notification.
  * A user in MULTIPLE affected groups still gets one notification
    (deduplication across the supplied group_ids list).
  * Groups with no members produce zero notifications, no error.
  * Notification rows carry the expected `notification_type`,
    `title`, `body`, and `metadata` shape.
  * Empty `group_ids` short-circuits to a no-op (no DB queries).
  * Unknown `resource_kind` produces a notification with a sensible
    fallback label rather than crashing.
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
from ulid import ULID

from app.database import Base
from app.database.models.admin import TenantRole
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.notification import Notification
from app.database.models.resource_group import ResourceGroup
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group_membership import (
    ResourceGroupMembership,
)
from app.database.models.tenant import Tenant
from app.services.notifications.group_resources import (
    NOTIFICATION_TYPE,
    notify_group_members_about_resource_change,
)


# Tenant has an FK to llm_model.id (default_embedding_model_id) and
# llm_model has an FK to llm_providers.id — both must be
# created so the SQLite engine accepts the Tenant row even though
# we don't reference them in the assertions.
REQUIRED_TABLES = [
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    LlmCatalog.__table__,   # LLModel.catalog_id FKs here
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,    # EndUser.role_id FK
    IdPServer.__table__,     # EndUser.idp_id FK
    ResourceGroup.__table__,
    EndUser.__table__,        # FK→tenants, role, primary_group, idp
    ResourceGroupMembership.__table__,
    Notification.__table__,
    # ResourceGroup.end_user_groups selectin needs these.
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

    Factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True,
    )
    s = Factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _tenant(s, tid="t1"):
    t = Tenant(
        id=tid, name=tid, display_name=tid.upper(),
        organization_name=tid.upper(),
        primary_contact_email=f"a@{tid}.example",
        is_active=True, is_default=False, is_trial=False,
        suspended=False, billing_plan="self_hosted",
    )
    s.add(t)
    s.flush()
    return t


def _group(s, tenant_id, name, display_name=None):
    g = ResourceGroup(
        id=f"rg-{ULID()}",
        tenant_id=tenant_id,
        name=name,
        display_name=display_name or name,
        hierarchy_level=0,
        hierarchy_path=name,
        inherit_resources=True,
        is_active=True,
    )
    s.add(g)
    s.flush()
    return g


def _user(s, tenant_id, user_id, username=None):
    """Create an EndUser if it doesn't already exist for this id.
    The membership row FKs into `end_users.id`."""
    existing = s.query(EndUser).filter(EndUser.id == user_id).first()
    if existing is not None:
        return existing
    u = EndUser(
        id=user_id,
        tenant_id=tenant_id,
        username=username or f"user{user_id}",
        email=f"user{user_id}@example.test",
        password_hash="x",
        is_verified=True,
        is_active=True,
    )
    s.add(u)
    s.flush()
    return u


def _join(s, tenant_id, user_id, group):
    _user(s, tenant_id, user_id)
    m = ResourceGroupMembership(
        tenant_id=tenant_id,
        user_id=user_id,
        group_id=group.id,
    )
    s.add(m)
    s.flush()
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNotifyGroupMembers:
    def test_one_notification_per_member(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "engineering", "Engineering")
        for uid in (101, 102, 103):
            _join(session, "t1", uid, g)
        session.flush()

        n = notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g.id],
            resource_kind="model",
            resource_id="llm-1",
            resource_name="GPT-4o",
            action="added",
            actor_username="alice",
        )
        session.commit()

        assert n == 3
        rows = session.query(Notification).order_by(Notification.user_id).all()
        assert len(rows) == 3
        assert {r.user_id for r in rows} == {101, 102, 103}
        for r in rows:
            assert r.tenant_id == "t1"
            assert r.notification_type == NOTIFICATION_TYPE
            assert "GPT-4o" in r.title
            assert "Engineering" in r.title
            assert r.notification_metadata["action"] == "added"
            assert r.notification_metadata["resource_kind"] == "model"
            assert r.notification_metadata["resource_id"] == "llm-1"
            assert r.notification_metadata["actor"] == "alice"

    def test_dedup_across_multiple_groups(self, session):
        # User 101 is in BOTH groups; user 102 in only g1; user 103 in only g2.
        # When we notify about a resource added to BOTH groups, user 101
        # should still get exactly one row.
        _tenant(session, "t1")
        g1 = _group(session, "t1", "g1", "Alpha")
        g2 = _group(session, "t1", "g2", "Beta")
        _join(session, "t1", 101, g1)
        _join(session, "t1", 101, g2)
        _join(session, "t1", 102, g1)
        _join(session, "t1", 103, g2)
        session.flush()

        n = notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g1.id, g2.id],
            resource_kind="agent_skill",
            resource_id="skill-1",
            resource_name="Refund Helper",
            action="added",
            actor_username="bob",
        )
        session.commit()

        assert n == 3, "expected one row per distinct user, got {}".format(n)
        rows = session.query(Notification).all()
        assert {r.user_id for r in rows} == {101, 102, 103}
        # All rows should reference both groups in the metadata.
        for r in rows:
            assert set(r.notification_metadata["group_ids"]) == {g1.id, g2.id}
            assert "Alpha" in r.notification_metadata["group_names"]
            assert "Beta" in r.notification_metadata["group_names"]

    def test_no_members_no_notifications(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "empty", "Empty")
        # No memberships.
        n = notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g.id],
            resource_kind="mcp_server",
            resource_id="srv-1",
            resource_name="Postgres",
            action="removed",
            actor_username="carol",
        )
        session.commit()
        assert n == 0
        assert session.query(Notification).count() == 0

    def test_empty_group_ids_is_noop(self, session):
        _tenant(session, "t1")
        n = notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[],
            resource_kind="model",
            resource_id="x",
            resource_name="x",
            action="added",
            actor_username=None,
        )
        assert n == 0

    def test_unknown_kind_uses_fallback_label(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "ops", "Ops")
        _join(session, "t1", 1, g)
        n = notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g.id],
            resource_kind="weird_kind",
            resource_id="r1",
            resource_name="Something",
            action="added",
            actor_username=None,
        )
        session.commit()
        assert n == 1
        row = session.query(Notification).first()
        # Falls back to "weird kind" (underscores → spaces) rather
        # than crashing on a missing label.
        assert "weird kind" in row.title
        assert "Something" in row.title

    def test_removed_action_title_says_removed(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "support", "Support")
        _join(session, "t1", 7, g)
        notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g.id],
            resource_kind="kb_collection",
            resource_id="kb-1",
            resource_name="Customer Docs",
            action="removed",
            actor_username="dave",
        )
        session.commit()
        row = session.query(Notification).one()
        assert "removed" in row.title.lower()
        assert "Customer Docs" in row.title
        assert row.notification_metadata["action"] == "removed"

    def test_actor_appears_in_body(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "ops")
        _join(session, "t1", 1, g)
        notify_group_members_about_resource_change(
            db=session,
            tenant_id="t1",
            group_ids=[g.id],
            resource_kind="model",
            resource_id="m1",
            resource_name="Claude",
            action="added",
            actor_username="erin",
        )
        session.commit()
        row = session.query(Notification).one()
        assert "**By:** erin" in row.body

    def test_invalid_action_raises(self, session):
        _tenant(session, "t1")
        g = _group(session, "t1", "ops")
        _join(session, "t1", 1, g)
        with pytest.raises(AssertionError):
            notify_group_members_about_resource_change(
                db=session,
                tenant_id="t1",
                group_ids=[g.id],
                resource_kind="model",
                resource_id="m1",
                resource_name="x",
                action="updated",  # not allowed
                actor_username=None,
            )
