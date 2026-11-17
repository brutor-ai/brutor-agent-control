# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# LLMAccessService EndUserGroup edge tests

Verifies `LLMAccessService.get_user_group_ids()` correctly unions:
  1. Direct user→RG memberships.
  2. Indirect user→EndUserGroup→RG.

These are pure DB tests against in-memory SQLite — every other
caller of `get_user_group_ids` (and its downstream:
`get_accessible_models`, `check_user_access`, etc.) inherits the
correct behaviour for free, but the union logic deserves its own
focused test so a regression here is immediately visible.

Tenant isolation matters too: a user-group binding in a different
tenant must NOT leak through the join.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event
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
from app.database.models.llm_catalog import LlmCatalog
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from app.services.end_user_group_manager import EndUserGroupManager
from app.services.llm.model.llm_access_service import LLMAccessService


REQUIRED_TABLES = [
    LlmCatalog.__table__,   # LLModel.catalog_id FKs here
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
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    s = Session()
    for tid, dn in [("t1", "T1"), ("t2", "T2")]:
        s.add(Tenant(
            id=tid, name=tid, display_name=dn,
            primary_contact_email=f"a@{tid}.invalid",
        ))
    s.flush()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(db, *, tenant_id="t1", username):
    u = EndUser(
        tenant_id=tenant_id, username=username,
        email=f"{username}@example.invalid",
        password_hash="x" * 60,
    )
    db.add(u)
    db.flush()
    return u


def _rg(db, *, rg_id, tenant_id="t1", name=None):
    rg = ResourceGroup(
        id=rg_id, tenant_id=tenant_id,
        name=name or rg_id, display_name=name or rg_id,
    )
    db.add(rg)
    db.flush()
    return rg


def _direct_membership(db, *, tenant_id, user_id, group_id):
    db.add(ResourceGroupMembership(
        tenant_id=tenant_id,
        user_id=user_id,
        group_id=group_id,
    ))
    db.flush()


class TestGetUserGroupIdsUnion:
    def test_direct_membership_only(self, db):
        alice = _user(db, username="alice")
        rg = _rg(db, rg_id="rg-prod")
        _direct_membership(
            db, tenant_id="t1", user_id=alice.id, group_id=rg.id
        )
        db.commit()
        ids = LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        )
        assert ids == ["rg-prod"]

    def test_user_group_membership_only(self, db):
        alice = _user(db, username="alice")
        rg = _rg(db, rg_id="rg-prod")
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()

        ids = LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        )
        assert ids == ["rg-prod"]

    def test_union_deduplicates(self, db):
        # User reachable via BOTH direct AND user-group → only one
        # RG id in the result.
        alice = _user(db, username="alice")
        rg = _rg(db, rg_id="rg-prod")
        _direct_membership(
            db, tenant_id="t1", user_id=alice.id, group_id=rg.id
        )
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()

        ids = LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        )
        assert ids == ["rg-prod"]

    def test_multiple_rgs_via_two_edges(self, db):
        # rg-direct via direct, rg-via-group via user-group.
        alice = _user(db, username="alice")
        rg_direct = _rg(db, rg_id="rg-direct")
        rg_indirect = _rg(db, rg_id="rg-via-group")
        _direct_membership(
            db, tenant_id="t1", user_id=alice.id,
            group_id=rg_direct.id,
        )
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg_indirect.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()
        ids = sorted(
            LLMAccessService.get_user_group_ids(
                db, user_id=alice.id, tenant_id="t1"
            )
        )
        assert ids == ["rg-direct", "rg-via-group"]

    def test_other_users_in_same_group_not_returned(self, db):
        # Alice and Bob both in the same EndUserGroup, but the query
        # for Alice's RG access must NOT include rows keyed on Bob.
        # (Regression guard against a join that drops the user filter.)
        alice = _user(db, username="alice")
        bob = _user(db, username="bob")
        rg = _rg(db, rg_id="rg-prod")
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id, bob.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()

        # Alice sees rg-prod.
        assert LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        ) == ["rg-prod"]
        # Bob sees the same RG (independently).
        assert LLMAccessService.get_user_group_ids(
            db, user_id=bob.id, tenant_id="t1"
        ) == ["rg-prod"]

    def test_cross_tenant_isolation(self, db):
        # Tenant t2 user, t2 RG, t2 group. The t1 query for the
        # same numeric user_id (impossible in practice, but defends
        # the join) must NOT return t2's RG.
        alice_t2 = _user(db, tenant_id="t2", username="alice")
        rg_t2 = _rg(db, rg_id="rg-t2", tenant_id="t2")
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t2", name="eng",
            initial_member_ids=[alice_t2.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t2", resource_group_id=rg_t2.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()
        # Same user_id, wrong tenant.
        ids = LLMAccessService.get_user_group_ids(
            db, user_id=alice_t2.id, tenant_id="t1"
        )
        assert ids == []
        # Real tenant — works.
        ids = LLMAccessService.get_user_group_ids(
            db, user_id=alice_t2.id, tenant_id="t2"
        )
        assert ids == ["rg-t2"]

    def test_user_not_in_any_group_returns_empty(self, db):
        alice = _user(db, username="alice")
        db.commit()
        assert LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        ) == []

    def test_removing_from_user_group_revokes_rg_access(self, db):
        # The whole point of EndUserGroup for SAML/SCIM: removing the
        # user from the IdP's group revokes their RG access on the
        # next access check (no caching at this layer).
        alice = _user(db, username="alice")
        rg = _rg(db, rg_id="rg-prod")
        mgr = EndUserGroupManager()
        ug = mgr.create_manual(
            db, tenant_id="t1", name="eng",
            initial_member_ids=[alice.id],
        )
        mgr.bind_to_resource_group(
            db, tenant_id="t1", resource_group_id=rg.id,
            end_user_group_ids=[ug.id],
        )
        db.commit()
        assert LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        ) == ["rg-prod"]

        # Now strip Alice from the group — RG access drops.
        mgr.remove_members(
            db, tenant_id="t1", group_id=ug.id,
            end_user_ids=[alice.id],
        )
        db.commit()
        assert LLMAccessService.get_user_group_ids(
            db, user_id=alice.id, tenant_id="t1"
        ) == []
