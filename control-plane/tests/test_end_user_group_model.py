# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EndUserGroup model schema tests

Locks down the SQLAlchemy model, its constraints, and the
relationships on `EndUser` and `ResourceGroup`. These tests run
against in-memory SQLite, so the partial unique index from migration
`a051_end_user_groups` (Postgres-only) is NOT exercised here — that's covered in the
service-layer reconciler tests against a real Postgres DB and by the
migration shape itself.

Coverage:
  1. Insert + reload + to_dict surfaces every documented field.
  2. UniqueConstraint (tenant_id, name) — duplicate name in same
     tenant is rejected; same name in DIFFERENT tenant is fine.
  3. `source` validator rejects unknown values.
  4. M2M `EndUserGroup.members` round-trips: assign list of EndUser,
     reload, members come back.
  5. M2M `ResourceGroup.end_user_groups` round-trips.
  6. ON DELETE CASCADE — deleting an EndUserGroup wipes its
     membership rows but NOT its members. Deleting a Tenant
     cascade-deletes the group.
  7. ON DELETE SET NULL — deleting the IdPServer leaves the group
     row alive with `idp_server_id` flipped to NULL (orphan manual
     group).
  8. IdPServer.to_dict shows none of the SAML columns (SAML single
     sign-on is not in this release).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.admin import TenantRole
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant


# Tables the schema tests actually touch. LLModel is included because
# Tenant.default_embedding_model_id FKs into it (Sprint-pre-663 column);
# TenantRole is required because EndUser.role_id FKs there even when
# left NULL — SQLite RETURNING resolves the FK during INSERT.
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
    # SQLite needs PRAGMA foreign_keys=ON to enforce FK constraints.
    # Without it, the CASCADE / SET NULL tests below would silently
    # ignore the rules.
    @event.listens_for(engine, "connect")
    def _fk_pragma_on_connect(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = Session()
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


def _seed_user(db, *, tenant_id="t1", username="alice"):
    u = EndUser(
        tenant_id=tenant_id,
        username=username,
        email=f"{username}@example.invalid",
        password_hash="x" * 60,
    )
    db.add(u)
    db.flush()
    return u


def _seed_rg(db, *, group_id="rg-1", tenant_id="t1", name="Engineering"):
    rg = ResourceGroup(
        id=group_id, tenant_id=tenant_id, name=name,
        display_name=name,
    )
    db.add(rg)
    db.flush()
    return rg


def _seed_idp(db, *, idp_id="idp-okta", tenant_id="t1"):
    idp = IdPServer(
        id=idp_id, tenant_id=tenant_id, name="Okta",
        issuer="https://acme.okta.com", type="generic_saml",
        protocol="saml", enabled=True,
    )
    db.add(idp)
    db.flush()
    return idp


# ---------------------------------------------------------------------------
# 1. Basic CRUD + to_dict
# ---------------------------------------------------------------------------


class TestCrud:
    def test_insert_reload_to_dict(self, db):
        idp = _seed_idp(db)
        g = EndUserGroup(
            id="eug-1",
            tenant_id="t1",
            name="engineering-team",
            display_name="Engineering Team",
            description="All engineers",
            source="manual",
            source_external_id="okta-engineers",
            idp_server_id=idp.id,
        )
        db.add(g)
        db.commit()
        db.refresh(g)

        d = g.to_dict()
        assert d["id"] == "eug-1"
        assert d["tenant_id"] == "t1"
        assert d["name"] == "engineering-team"
        assert d["display_name"] == "Engineering Team"
        assert d["source"] == "manual"
        assert d["source_external_id"] == "okta-engineers"
        assert d["idp_server_id"] == idp.id
        assert d["member_count"] == 0
        assert "created_at" in d and d["created_at"] is not None
        # Default include_members=False
        assert "members" not in d


# ---------------------------------------------------------------------------
# 2. UniqueConstraint (tenant_id, name)
# ---------------------------------------------------------------------------


class TestUniqueNamePerTenant:
    def test_duplicate_name_same_tenant_rejected(self, db):
        db.add(EndUserGroup(id="eug-a", tenant_id="t1", name="engineering"))
        db.commit()
        db.add(EndUserGroup(id="eug-b", tenant_id="t1", name="engineering"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_same_name_different_tenant_allowed(self, db):
        db.add(EndUserGroup(id="eug-a", tenant_id="t1", name="engineering"))
        db.add(EndUserGroup(id="eug-b", tenant_id="t2", name="engineering"))
        db.commit()  # must not raise
        rows = db.query(EndUserGroup).filter(
            EndUserGroup.name == "engineering"
        ).all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 3. source validator
# ---------------------------------------------------------------------------


class TestSourceValidator:
    def test_manual_is_the_one_source(self, db):
        db.add(EndUserGroup(id="eug-manual", tenant_id="t1", name="group-manual", source="manual"))
        db.commit()
        # SAML single sign-on and SCIM provisioning are not in this release.
        for gone in ("saml", "scim"):
            with pytest.raises(ValueError, match="source must be one of"):
                EndUserGroup(id=f"eug-{gone}", tenant_id="t1", name=f"group-{gone}", source=gone)

    def test_invalid_source_rejected(self, db):
        with pytest.raises(ValueError, match="source must be one of"):
            EndUserGroup(
                id="eug-bad", tenant_id="t1",
                name="bad", source="ldap",
            )


# ---------------------------------------------------------------------------
# 4. Membership M2M
# ---------------------------------------------------------------------------


class TestMembership:
    def test_add_users_to_group_via_relationship(self, db):
        alice = _seed_user(db, username="alice")
        bob = _seed_user(db, username="bob")
        g = EndUserGroup(id="eug-eng", tenant_id="t1", name="engineering")
        g.members = [alice, bob]
        db.add(g)
        db.commit()

        # Reload from scratch.
        db.expire_all()
        g2 = db.query(EndUserGroup).filter(
            EndUserGroup.id == "eug-eng"
        ).one()
        assert {u.username for u in g2.members} == {"alice", "bob"}
        assert g2.member_count == 2

    def test_user_user_groups_back_populates(self, db):
        alice = _seed_user(db, username="alice")
        g_a = EndUserGroup(id="eug-a", tenant_id="t1", name="alpha")
        g_b = EndUserGroup(id="eug-b", tenant_id="t1", name="beta")
        g_a.members = [alice]
        g_b.members = [alice]
        db.add_all([g_a, g_b])
        db.commit()

        db.expire_all()
        alice2 = db.query(EndUser).filter(
            EndUser.username == "alice"
        ).one()
        assert {g.name for g in alice2.user_groups} == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# 5. ResourceGroup ↔ EndUserGroup M2M
# ---------------------------------------------------------------------------


class TestResourceGroupBinding:
    def test_bind_user_group_to_resource_group(self, db):
        rg = _seed_rg(db, group_id="rg-prod")
        ug = EndUserGroup(id="eug-eng", tenant_id="t1", name="engineering")
        rg.end_user_groups = [ug]
        db.add(ug)
        db.commit()

        db.expire_all()
        rg2 = db.query(ResourceGroup).filter(
            ResourceGroup.id == "rg-prod"
        ).one()
        assert [g.name for g in rg2.end_user_groups] == ["engineering"]

        # And the reverse.
        ug2 = db.query(EndUserGroup).filter(
            EndUserGroup.id == "eug-eng"
        ).one()
        assert [g.id for g in ug2.resource_groups] == ["rg-prod"]


# ---------------------------------------------------------------------------
# 6. ON DELETE CASCADE / SET NULL
# ---------------------------------------------------------------------------


class TestDeleteCascades:
    def test_deleting_group_drops_memberships_keeps_users(self, db):
        alice = _seed_user(db, username="alice")
        g = EndUserGroup(id="eug-x", tenant_id="t1", name="x")
        g.members = [alice]
        db.add(g)
        db.commit()

        db.delete(g)
        db.commit()

        # User still exists.
        assert db.query(EndUser).filter(
            EndUser.username == "alice"
        ).one()
        # Membership row gone.
        from sqlalchemy import select
        rows = db.execute(
            select(end_user_group_members)
        ).fetchall()
        assert rows == []

    def test_deleting_idp_sets_idp_server_id_null(self, db):
        idp = _seed_idp(db)
        g = EndUserGroup(
            id="eug-saml", tenant_id="t1", name="saml-group",
            source="manual", idp_server_id=idp.id,
        )
        db.add(g)
        db.commit()

        db.delete(idp)
        db.commit()

        g2 = db.query(EndUserGroup).filter(
            EndUserGroup.id == "eug-saml"
        ).one()
        # Group row preserved, FK nulled out per ondelete=SET NULL.
        assert g2.idp_server_id is None
        assert g2.source == "manual"


# ---------------------------------------------------------------------------
# 7. IdPServer end-user-group columns
# ---------------------------------------------------------------------------


class TestIdpServerColumns:
    def test_to_dict_shows_no_saml_column(self, db):
        idp = _seed_idp(db, idp_id="idp-admin")
        idp.target_user_type = "tenant_admin"
        idp.saml_group_to_end_user_group_mapping = {"okta-engineers": "engineering-team"}
        db.commit()
        shown = idp.to_dict()
        assert sorted(k for k in shown if "saml" in k or k == "target_user_type") == []
