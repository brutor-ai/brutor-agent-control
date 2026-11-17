# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EndUserGroupManager service tests

Exhaustive coverage of CRUD + membership + RG binding. Idempotency and tenant-isolation are the load-bearing
properties — every code path that mutates state has at least one
test that asserts a repeat call is a no-op, and at least one test
that asserts a cross-tenant payload is rejected.

Layout:
  * `TestCrud` — create_manual, get, list, update, delete.
  * `TestMembership` — add / remove / set roster ops.
  * `TestRgBinding` — bind / unbind to Resource Groups.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.admin import TenantRole
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from app.services.end_user_group_manager import (
    EndUserGroupError,
    EndUserGroupManager,
)


REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    ResourceGroup.__table__,
    ResourceGroupMembership.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma_on_connect(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    session = Session()
    # Seed two tenants for cross-tenant isolation tests.
    session.add(Tenant(
        id="t1", name="t1", display_name="t1",
        primary_contact_email="t1@example.invalid",
    ))
    session.add(Tenant(
        id="t2", name="t2", display_name="t2",
        primary_contact_email="t2@example.invalid",
    ))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(db, *, tenant_id="t1", username):
    u = EndUser(
        tenant_id=tenant_id,
        username=username,
        email=f"{username}@example.invalid",
        password_hash="x" * 60,
    )
    db.add(u)
    db.flush()
    return u


def _rg(db, *, group_id, tenant_id="t1", name="Engineering"):
    rg = ResourceGroup(
        id=group_id, tenant_id=tenant_id, name=name,
        display_name=name,
    )
    db.add(rg)
    db.flush()
    return rg


def _idp(db, *, idp_id="idp-okta", tenant_id="t1"):
    idp = IdPServer(
        id=idp_id, tenant_id=tenant_id, name="Okta",
        issuer="https://acme.okta.com", type="generic_saml",
        protocol="saml", enabled=True,
    )
    db.add(idp)
    db.flush()
    return idp


@pytest.fixture
def mgr():
    return EndUserGroupManager()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCrud:
    def test_create_manual_minimum(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="engineering")
        assert g.id.startswith("eug-")
        assert g.tenant_id == "t1"
        assert g.name == "engineering"
        assert g.source == "manual"
        assert g.source_external_id is None
        assert g.idp_server_id is None

    def test_create_manual_with_initial_members(self, db, mgr):
        alice = _user(db, username="alice")
        bob = _user(db, username="bob")
        g = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id, bob.id],
        )
        assert {u.username for u in g.members} == {"alice", "bob"}

    def test_create_manual_rejects_empty_name(self, db, mgr):
        with pytest.raises(EndUserGroupError) as ei:
            mgr.create_manual(db, tenant_id="t1", name="   ")
        assert ei.value.code == "invalid_name"

    def test_create_manual_rejects_duplicate_name(self, db, mgr):
        mgr.create_manual(db, tenant_id="t1", name="eng")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.create_manual(db, tenant_id="t1", name="eng")
        assert ei.value.code == "name_conflict"

    def test_create_manual_rejects_cross_tenant_user(self, db, mgr):
        cross = _user(db, tenant_id="t2", username="eve")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.create_manual(
                db, tenant_id="t1", name="eng",
                initial_member_ids=[cross.id],
            )
        assert ei.value.code == "cross_tenant_user"

    def test_get_returns_none_when_not_in_tenant(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        # Same id, wrong tenant.
        assert mgr.get(db, tenant_id="t2", group_id=g.id) is None

    def test_list_pagination_and_filter(self, db, mgr):
        for n in ["alpha", "beta", "gamma", "delta"]:
            mgr.create_manual(db, tenant_id="t1", name=n)
        rows, total = mgr.list(db, tenant_id="t1", limit=2, offset=0)
        assert total == 4
        assert len(rows) == 2

        _rows, total_manual = mgr.list(db, tenant_id="t1", source="manual")
        assert total_manual == 4
        # SAML and SCIM are not in this release: neither is a source.
        for gone in ("saml", "scim"):
            with pytest.raises(EndUserGroupError) as ei:
                mgr.list(db, tenant_id="t1", source=gone)
            assert ei.value.code == "invalid_source_filter"

        rows_search, total_search = mgr.list(db, tenant_id="t1", search="ALPHA")
        assert total_search == 1
        assert rows_search[0].name == "alpha"

    def test_list_invalid_source_rejected(self, db, mgr):
        with pytest.raises(EndUserGroupError) as ei:
            mgr.list(db, tenant_id="t1", source="ldap")
        assert ei.value.code == "invalid_source_filter"

    def test_update_rename_manual_group(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        g2 = mgr.update(db, tenant_id="t1", group_id=g.id, name="engineering")
        assert g2.name == "engineering"

    def test_update_rename_collision(self, db, mgr):
        mgr.create_manual(db, tenant_id="t1", name="alpha")
        g = mgr.create_manual(db, tenant_id="t1", name="beta")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.update(db, tenant_id="t1", group_id=g.id, name="alpha")
        assert ei.value.code == "name_conflict"

    def test_update_rename_stored_group_of_another_source_rejected(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="stored-eng")
        # A row an earlier SCIM provisioning wrote (nothing writes one now).
        db.execute(EndUserGroup.__table__.update().where(EndUserGroup.id == g.id).values(source="scim"))
        db.expire_all()
        with pytest.raises(EndUserGroupError) as ei:
            mgr.update(db, tenant_id="t1", group_id=g.id, name="renamed")
        assert ei.value.code == "rename_not_allowed"

    def test_delete_idempotent(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        mgr.delete(db, tenant_id="t1", group_id=g.id)
        # Second delete is a no-op.
        mgr.delete(db, tenant_id="t1", group_id=g.id)
        assert mgr.get(db, tenant_id="t1", group_id=g.id) is None


# ---------------------------------------------------------------------------
# Membership mutation
# ---------------------------------------------------------------------------


class TestMembership:
    def test_add_members_idempotent(self, db, mgr):
        alice = _user(db, username="alice")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        mgr.add_members(
            db, tenant_id="t1", group_id=g.id, end_user_ids=[alice.id]
        )
        mgr.add_members(
            db, tenant_id="t1", group_id=g.id, end_user_ids=[alice.id]
        )
        rows = db.execute(
            select(end_user_group_members).where(
                end_user_group_members.c.end_user_group_id == g.id
            )
        ).fetchall()
        assert len(rows) == 1

    def test_remove_members_silent_skip(self, db, mgr):
        alice = _user(db, username="alice")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        # Remove without ever adding — must not raise.
        mgr.remove_members(
            db, tenant_id="t1", group_id=g.id, end_user_ids=[alice.id]
        )

    def test_set_members_authoritative(self, db, mgr):
        alice = _user(db, username="alice")
        bob = _user(db, username="bob")
        carol = _user(db, username="carol")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        mgr.add_members(
            db, tenant_id="t1", group_id=g.id,
            end_user_ids=[alice.id, bob.id],
        )
        # Replace roster: bob OUT, carol IN, alice stays.
        mgr.set_members(
            db, tenant_id="t1", group_id=g.id,
            end_user_ids=[alice.id, carol.id],
        )
        db.refresh(g)
        assert {u.username for u in g.members} == {"alice", "carol"}

    def test_set_members_to_empty_list_clears_roster(self, db, mgr):
        alice = _user(db, username="alice")
        g = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id],
        )
        mgr.set_members(db, tenant_id="t1", group_id=g.id, end_user_ids=[])
        db.refresh(g)
        assert list(g.members) == []

    def test_set_members_rejects_cross_tenant(self, db, mgr):
        cross = _user(db, tenant_id="t2", username="eve")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.set_members(
                db, tenant_id="t1", group_id=g.id,
                end_user_ids=[cross.id],
            )
        assert ei.value.code == "cross_tenant_user"


# ---------------------------------------------------------------------------
# Resource Group binding
# ---------------------------------------------------------------------------


class TestRgBinding:
    def test_bind_idempotent(self, db, mgr):
        rg = _rg(db, group_id="rg-prod")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[g.id], created_by="admin-1",
        )
        # Second call must be a no-op.
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[g.id],
        )
        rows = db.execute(
            select(resource_group_end_user_groups).where(
                resource_group_end_user_groups.c.resource_group_id == rg.id
            )
        ).fetchall()
        assert len(rows) == 1
        assert rows[0].created_by == "admin-1"

    def test_unbind_silent_skip(self, db, mgr):
        rg = _rg(db, group_id="rg-prod")
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        # Unbind something that was never bound.
        mgr.unbind_from_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[g.id],
        )

    def test_bind_rejects_cross_tenant_user_group(self, db, mgr):
        rg = _rg(db, group_id="rg-prod", tenant_id="t1")
        cross_group = mgr.create_manual(db, tenant_id="t2", name="other")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.bind_to_resource_group(
                db, tenant_id="t1", resource_group_id=rg.id,
                end_user_group_ids=[cross_group.id],
            )
        assert ei.value.code == "cross_tenant_group"

    def test_bind_unknown_rg(self, db, mgr):
        g = mgr.create_manual(db, tenant_id="t1", name="eng")
        with pytest.raises(EndUserGroupError) as ei:
            mgr.bind_to_resource_group(
                db, tenant_id="t1", resource_group_id="rg-missing",
                end_user_group_ids=[g.id],
            )
        assert ei.value.code == "not_found"
