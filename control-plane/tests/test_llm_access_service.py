# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for LLMAccessService — the decision layer for which LLM models a
tenant / group / user can see. This is the authoritative source for
multi-tenant isolation on the model side: every chat request's model
lookup funnels through these helpers.

Covers:
  - resolve_default_group_id — lookup + error when missing
  - resolve_group_ids — sentinel substitution, deduplication, order
  - get_user_group_ids / get_user_groups — membership lookup
  - check_user_access — inheritance + enabled gate + unknown model
  - assign/add/remove_model_to_groups — round-trip, sentinel, Default-protected
  - get_group_models — direct vs inherited

Run: pytest tests/test_llm_access_service.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.constants.groups import ResourceGroupName
from app.database import Base
from app.database.models.llm_model import LLModel
from app.database.models.llm_runner_machine import (
    LLMRunnerMachine,
    resource_group_deployment_machines,
)
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
)
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from app.services.llm.model.llm_access_service import (
    DEFAULT_GROUP_SENTINEL,
    LLMAccessService,
)


def _llm_provider_table():
    from app.database.models.llm_provider import LlmProvider
    return LlmProvider.__table__


def _llm_provider_catalog_table():
    from app.database.models.llm_provider_catalog import LlmProviderCatalog
    return LlmProviderCatalog.__table__


REQUIRED_TABLES = [
    Tenant.__table__,
    # LLModel.provider_id FKs into llm_providers — table
    # must exist before LLModel rows are inserted. llm_providers in turn
    # FKs into llm_provider_catalog (catalog_id), so that table comes first.
    _llm_provider_catalog_table(),
    _llm_provider_table(),
    ResourceGroup.__table__,
    LLModel.__table__,
    LLMRunnerMachine.__table__,
    ResourceGroupMembership.__table__,
    resource_group_llm_models,
    resource_group_deployment_machines,
    # LLMAccessService.get_user_group_ids() unions
    # direct memberships with EndUserGroup-mediated access. The query
    # must succeed (even if it returns zero rows) for every fixture
    # that goes through the access service, so the three EndUserGroup
    # tables have to exist alongside the legacy ones.
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


TENANT_ID = "t-access"


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        tenant = Tenant(
            id=TENANT_ID,
            name=TENANT_ID,
            display_name="Access Test",
            organization_name="Access",
            primary_contact_email="admin@access.test",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
        )
        s.add(tenant)
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _mk_group(
    db: Session,
    name: str,
    is_system: bool = False,
    parent_id: str | None = None,
    inherit_resources: bool = True,
) -> ResourceGroup:
    g = ResourceGroup(
        id=f"g-{uuid.uuid4().hex[:10]}",
        tenant_id=TENANT_ID,
        name=name,
        hierarchy_path=name,
        hierarchy_level=0 if parent_id is None else 1,
        parent_group_id=parent_id,
        is_system=is_system,
        is_active=True,
        inherit_resources=inherit_resources,
    )
    db.add(g)
    db.flush()
    return g


def _mk_provider(db: Session, key: str = "openai"):
    """Providers are first-class. Each test that mints
    a model needs a provider row first to satisfy the FK."""
    from app.database.models.llm_provider import LlmProvider
    existing = (
        db.query(LlmProvider)
        .filter(LlmProvider.tenant_id == TENANT_ID, LlmProvider.key == key)
        .first()
    )
    if existing:
        return existing
    p = LlmProvider(
        id=f"prov-{uuid.uuid4().hex[:10]}",
        tenant_id=TENANT_ID,
        key=key,
        display_name=key.title(),
        enabled=True,
        is_system=True,
    )
    db.add(p)
    db.flush()
    return p


def _mk_model(db: Session, name: str, enabled: bool = True) -> LLModel:
    provider = _mk_provider(db, "openai")
    m = LLModel(
        id=f"m-{uuid.uuid4().hex[:10]}",
        tenant_id=TENANT_ID,
        name=name,
        model_name=name,
        provider_id=provider.id,
        mode="chat",
        enabled=enabled,
    )
    db.add(m)
    db.flush()
    return m


def _assign(db: Session, group_id: str, model_id: str):
    db.execute(
        resource_group_llm_models.insert().values(
            resource_group_id=group_id, llm_model_id=model_id
        )
    )


# =============================================================================
# resolve_default_group_id
# =============================================================================


class TestResolveDefaultGroup:
    def test_returns_id_of_the_default_system_group(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        _mk_group(db, "Other")
        db.commit()

        got = LLMAccessService.resolve_default_group_id(db, TENANT_ID)
        assert got == default.id

    def test_raises_when_no_default_group_exists(self, db: Session):
        with pytest.raises(ValueError, match="Default system group not found"):
            LLMAccessService.resolve_default_group_id(db, TENANT_ID)

    def test_non_system_default_named_group_is_ignored(self, db: Session):
        # A user created a group literally named "Default" but is_system=False.
        # Must NOT be returned as the system Default.
        _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=False)
        db.commit()

        with pytest.raises(ValueError):
            LLMAccessService.resolve_default_group_id(db, TENANT_ID)


# =============================================================================
# resolve_group_ids — sentinel substitution, dedup, order
# =============================================================================


class TestResolveGroupIds:
    def test_substitutes_default_sentinel(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        db.commit()

        out = LLMAccessService.resolve_group_ids(
            db, [DEFAULT_GROUP_SENTINEL], TENANT_ID
        )
        assert out == [default.id]

    def test_preserves_input_order_and_dedupes(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        other = _mk_group(db, "Other")
        db.commit()

        out = LLMAccessService.resolve_group_ids(
            db,
            [other.id, DEFAULT_GROUP_SENTINEL, other.id, default.id],
            TENANT_ID,
        )
        # Sentinel and default.id collapse. The 'other.id' second occurrence
        # is dropped. Result preserves first-seen ordering.
        assert out == [other.id, default.id]


# =============================================================================
# User group membership
# =============================================================================


class TestUserGroupMembership:
    def test_get_user_group_ids_returns_empty_when_no_memberships(
        self, db: Session
    ):
        assert LLMAccessService.get_user_group_ids(db, "u-0", TENANT_ID) == []

    def test_get_user_group_ids_returns_only_that_users_memberships(
        self, db: Session
    ):
        g1 = _mk_group(db, "G1")
        g2 = _mk_group(db, "G2")
        db.add_all(
            [
                ResourceGroupMembership(
                    tenant_id=TENANT_ID, user_id="u-1", group_id=g1.id, role="member"
                ),
                ResourceGroupMembership(
                    tenant_id=TENANT_ID, user_id="u-1", group_id=g2.id, role="member"
                ),
                ResourceGroupMembership(
                    tenant_id=TENANT_ID, user_id="u-2", group_id=g1.id, role="member"
                ),
            ]
        )
        db.commit()

        got = set(LLMAccessService.get_user_group_ids(db, "u-1", TENANT_ID))
        assert got == {g1.id, g2.id}

    def test_get_user_groups_returns_resource_group_rows(self, db: Session):
        g1 = _mk_group(db, "G1")
        db.add(
            ResourceGroupMembership(
                tenant_id=TENANT_ID, user_id="u-1", group_id=g1.id, role="member"
            )
        )
        db.commit()
        groups = LLMAccessService.get_user_groups(db, "u-1", TENANT_ID)
        assert [g.id for g in groups] == [g1.id]


# =============================================================================
# check_user_access — inheritance + enabled gate
# =============================================================================


class TestCheckUserAccess:
    def test_direct_group_assignment_grants_access(self, db: Session):
        g = _mk_group(db, "G1")
        m = _mk_model(db, "gpt-4o")
        _assign(db, g.id, m.id)
        db.add(
            ResourceGroupMembership(
                tenant_id=TENANT_ID, user_id="u-1", group_id=g.id, role="member"
            )
        )
        db.commit()

        assert LLMAccessService.check_user_access(db, TENANT_ID, "u-1", m.id) is True

    def test_unknown_model_returns_false(self, db: Session):
        assert (
            LLMAccessService.check_user_access(db, TENANT_ID, "u-1", "m-ghost")
            is False
        )

    def test_disabled_model_denies_access(self, db: Session):
        g = _mk_group(db, "G1")
        m = _mk_model(db, "disabled-model", enabled=False)
        _assign(db, g.id, m.id)
        db.add(
            ResourceGroupMembership(
                tenant_id=TENANT_ID, user_id="u-1", group_id=g.id, role="member"
            )
        )
        db.commit()

        assert LLMAccessService.check_user_access(db, TENANT_ID, "u-1", m.id) is False

    def test_user_with_no_group_membership_has_no_access(self, db: Session):
        g = _mk_group(db, "G1")
        m = _mk_model(db, "gpt-4o")
        _assign(db, g.id, m.id)
        db.commit()
        # "u-orphan" has no memberships.
        assert (
            LLMAccessService.check_user_access(db, TENANT_ID, "u-orphan", m.id)
            is False
        )

    def test_inherited_model_from_parent_group_grants_access(self, db: Session):
        parent = _mk_group(db, "Parent")
        child = _mk_group(
            db, "Child", parent_id=parent.id, inherit_resources=True
        )
        m = _mk_model(db, "inherited")
        _assign(db, parent.id, m.id)  # Only on parent.
        db.add(
            ResourceGroupMembership(
                tenant_id=TENANT_ID,
                user_id="u-child",
                group_id=child.id,
                role="member",
            )
        )
        db.commit()

        # User is only in child group but inherits parent's models.
        assert (
            LLMAccessService.check_user_access(db, TENANT_ID, "u-child", m.id)
            is True
        )


# =============================================================================
# assign_model_to_groups — replaces existing assignments; uses resolve_group_ids
# =============================================================================


class TestAssignModelToGroups:
    def test_assign_replaces_existing_groups(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        g_old = _mk_group(db, "Old")
        g_new = _mk_group(db, "New")
        m = _mk_model(db, "gpt-4o")
        _assign(db, g_old.id, m.id)
        db.commit()

        LLMAccessService.assign_model_to_groups(db, m.id, [g_new.id], TENANT_ID)
        db.commit()

        # Old assignment gone, new one present. Default is NOT auto-injected
        # (caller's selection is respected — see docstring).
        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        gids = {r.resource_group_id for r in rows}
        assert gids == {g_new.id}

    def test_assign_preserves_limits_and_portal_visible_for_surviving_groups(
        self, db: Session
    ):
        """Re-assigning a model must NOT wipe per-binding limits /
        portal_visible on groups that stay bound (regression for the
        delete-all + insert-bare behaviour)."""
        _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        g_keep = _mk_group(db, "Keep")
        g_drop = _mk_group(db, "Drop")
        g_new = _mk_group(db, "New")
        m = _mk_model(db, "gpt-4o")
        # g_keep carries operator-configured per-binding data; g_drop is bare.
        db.execute(
            resource_group_llm_models.insert().values(
                resource_group_id=g_keep.id,
                llm_model_id=m.id,
                limits={"max_requests_per_day": 100},
                portal_visible=False,
            )
        )
        _assign(db, g_drop.id, m.id)
        db.commit()

        # Replace the set: keep g_keep, drop g_drop, add g_new.
        LLMAccessService.assign_model_to_groups(
            db, m.id, [g_keep.id, g_new.id], TENANT_ID
        )
        db.commit()

        rows = {
            r.resource_group_id: r
            for r in db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        }
        assert set(rows) == {g_keep.id, g_new.id}, "g_drop removed, g_new added"
        # Surviving binding kept its per-binding config.
        assert rows[g_keep.id].limits == {"max_requests_per_day": 100}
        assert not rows[g_keep.id].portal_visible, "portal_visible must survive re-assign"
        # New binding takes defaults.
        assert rows[g_new.id].limits is None
        assert rows[g_new.id].portal_visible, "new binding defaults to visible"

    def test_assign_resolves_sentinel_to_default_group(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        m = _mk_model(db, "gpt-4o")
        db.commit()

        LLMAccessService.assign_model_to_groups(
            db, m.id, [DEFAULT_GROUP_SENTINEL], TENANT_ID
        )
        db.commit()

        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        gids = {r.resource_group_id for r in rows}
        assert gids == {default.id}

    def test_assign_unknown_model_raises(self, db: Session):
        _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        db.commit()
        with pytest.raises(ValueError, match="Model .* not found"):
            LLMAccessService.assign_model_to_groups(
                db, "m-ghost", [DEFAULT_GROUP_SENTINEL], TENANT_ID
            )

    def test_assign_unknown_group_raises(self, db: Session):
        _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        m = _mk_model(db, "gpt-4o")
        db.commit()
        with pytest.raises(ValueError, match="Group .* not found"):
            LLMAccessService.assign_model_to_groups(
                db, m.id, ["g-ghost"], TENANT_ID
            )


# =============================================================================
# add_model_to_groups — additive; does not remove existing assignments
# =============================================================================


class TestAddModelToGroups:
    def test_add_keeps_existing_and_appends_new(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        g_extra = _mk_group(db, "Extra")
        m = _mk_model(db, "gpt-4o")
        _assign(db, default.id, m.id)
        db.commit()

        LLMAccessService.add_model_to_groups(db, m.id, [g_extra.id], TENANT_ID)
        db.commit()

        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        gids = {r.resource_group_id for r in rows}
        assert gids == {default.id, g_extra.id}

    def test_add_is_idempotent_for_existing_assignment(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        m = _mk_model(db, "gpt-4o")
        _assign(db, default.id, m.id)
        db.commit()

        LLMAccessService.add_model_to_groups(
            db, m.id, [DEFAULT_GROUP_SENTINEL], TENANT_ID
        )
        db.commit()

        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        # Still one row (no duplicate insert).
        assert len(rows) == 1


# =============================================================================
# remove_model_from_groups — Default-protected
# =============================================================================


class TestRemoveModelFromGroups:
    def test_remove_strips_specified_groups(self, db: Session):
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        g = _mk_group(db, "Custom")
        m = _mk_model(db, "gpt-4o")
        _assign(db, default.id, m.id)
        _assign(db, g.id, m.id)
        db.commit()

        LLMAccessService.remove_model_from_groups(db, m.id, [g.id], TENANT_ID)
        db.commit()

        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        gids = {r.resource_group_id for r in rows}
        assert gids == {default.id}

    def test_remove_ignores_default_group(self, db: Session):
        # The Default group must never be removable — otherwise a model
        # could become unreachable to all users.
        default = _mk_group(db, ResourceGroupName.DEFAULT.value, is_system=True)
        m = _mk_model(db, "gpt-4o")
        _assign(db, default.id, m.id)
        db.commit()

        LLMAccessService.remove_model_from_groups(
            db, m.id, [default.id, DEFAULT_GROUP_SENTINEL], TENANT_ID
        )
        db.commit()

        rows = (
            db.query(resource_group_llm_models)
            .filter(resource_group_llm_models.c.llm_model_id == m.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].resource_group_id == default.id


# =============================================================================
# get_group_models
# =============================================================================


class TestGetGroupModels:
    def test_returns_directly_assigned_models(self, db: Session):
        g = _mk_group(db, "G1")
        m1 = _mk_model(db, "A")
        m2 = _mk_model(db, "B")
        _assign(db, g.id, m1.id)
        _assign(db, g.id, m2.id)
        db.commit()

        models = LLMAccessService.get_group_models(
            db, g.id, TENANT_ID, enabled_only=True, include_inherited=False
        )
        assert {m.id for m in models} == {m1.id, m2.id}

    def test_enabled_only_filters_disabled(self, db: Session):
        g = _mk_group(db, "G1")
        m_on = _mk_model(db, "On", enabled=True)
        m_off = _mk_model(db, "Off", enabled=False)
        _assign(db, g.id, m_on.id)
        _assign(db, g.id, m_off.id)
        db.commit()

        models = LLMAccessService.get_group_models(
            db, g.id, TENANT_ID, enabled_only=True, include_inherited=False
        )
        assert {m.id for m in models} == {m_on.id}

    def test_include_inherited_walks_ancestors(self, db: Session):
        parent = _mk_group(db, "Parent")
        child = _mk_group(db, "Child", parent_id=parent.id, inherit_resources=True)
        m_parent = _mk_model(db, "parent-model")
        m_child = _mk_model(db, "child-model")
        _assign(db, parent.id, m_parent.id)
        _assign(db, child.id, m_child.id)
        db.commit()

        inherited = LLMAccessService.get_group_models(
            db, child.id, TENANT_ID, enabled_only=True, include_inherited=True
        )
        direct = LLMAccessService.get_group_models(
            db, child.id, TENANT_ID, enabled_only=True, include_inherited=False
        )
        assert {m.id for m in direct} == {m_child.id}
        assert {m.id for m in inherited} == {m_parent.id, m_child.id}
