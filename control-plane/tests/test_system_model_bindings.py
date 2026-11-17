# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Auto-binding of "system models" to resource groups.

The semantic-policy judge and the semantic-cache embedding model are
called on the caller's behalf and go through the ordinary resource-group
access check. These tests pin the three rules that make that workable
without inventing an access exemption:

  1. An existing binding is never touched — `portal_visible` included.
  2. A binding we create is hidden from the portal picker.
  3. Only `auto_bound` rows are reclaimed, and only when nothing needs
     them any more.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.kb_collection import KbCollection, resource_group_kb_collections
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_llm_models,
)
from app.database.models.semantic_cache_config import (
    SemanticCacheConfig,
    resource_group_cache_configs,
)
from app.database.models.semantic_policy import (
    SemanticPolicy,
    resource_group_semantic_policies,
)
from app.database.models.tenant import Tenant
from app.services.system_model_bindings import compute_required_bindings, sync_tenant

TENANT = "t-sysbind"
RG_A = "rg-a"
RG_B = "rg-b"
JUDGE = "m-judge"
EMBED = "m-embed"
OTHER = "m-other"

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    ResourceGroup.__table__,
    resource_group_llm_models,
    SemanticPolicy.__table__,
    resource_group_semantic_policies,
    SemanticCacheConfig.__table__,
    resource_group_cache_configs,
    KbCollection.__table__,
    resource_group_kb_collections,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    session: Session = sessionmaker(bind=engine, future=True)()

    # No LLModel rows are seeded on purpose: the binding sync never joins
    # to llm_model, it only moves model *ids* between config columns and
    # the association table. Seeding real rows would drag in llm_providers
    # (provider_id is a non-null FK) for no coverage benefit. The table
    # itself is created so the FK targets resolve.
    session.add(Tenant(
        id=TENANT, name=TENANT, display_name=TENANT,
        primary_contact_email="ops@example.com", is_default=True,
        default_embedding_model_id=EMBED,
    ))
    for rg in (RG_A, RG_B):
        session.add(ResourceGroup(id=rg, tenant_id=TENANT, name=rg, description=rg))
    session.commit()
    yield session
    session.close()


# ── helpers ────────────────────────────────────────────────────────────


def bindings(db):
    """{(rg, model): (portal_visible, auto_bound)}"""
    rows = db.execute(select(
        resource_group_llm_models.c.resource_group_id,
        resource_group_llm_models.c.llm_model_id,
        resource_group_llm_models.c.portal_visible,
        resource_group_llm_models.c.auto_bound,
    )).all()
    return {(r[0], r[1]): (bool(r[2]), bool(r[3])) for r in rows}


def add_policy(db, policy_id, groups, judge_model_id=JUDGE):
    db.add(SemanticPolicy(
        id=policy_id, tenant_id=TENANT, name=policy_id,
        constraint_text="no secrets",
        judge_model_id=judge_model_id,
    ))
    db.flush()
    for g in groups:
        db.execute(resource_group_semantic_policies.insert().values(
            resource_group_id=g, semantic_policy_id=policy_id))
    db.commit()


def add_cache_config(db, config_id, groups):
    db.add(SemanticCacheConfig(id=config_id, tenant_id=TENANT))
    db.flush()
    for g in groups:
        db.execute(resource_group_cache_configs.insert().values(
            resource_group_id=g, cache_config_id=config_id))
    db.commit()


def add_kb_collection(db, collection_id, groups):
    db.add(KbCollection(id=collection_id, tenant_id=TENANT, name=collection_id))
    db.flush()
    for g in groups:
        db.execute(resource_group_kb_collections.insert().values(
            resource_group_id=g, kb_collection_id=collection_id))
    db.commit()


def unbind_policy_groups(db, policy_id):
    db.execute(resource_group_semantic_policies.delete().where(
        resource_group_semantic_policies.c.semantic_policy_id == policy_id))
    db.commit()


# ── semantic policy ────────────────────────────────────────────────────


def test_judge_model_is_bound_hidden_to_policy_groups(db):
    add_policy(db, "p1", [RG_A])
    stats = sync_tenant(db, TENANT)
    db.commit()

    assert stats["added"] == 1
    # Access granted, but kept out of the end-user picker.
    assert bindings(db)[(RG_A, JUDGE)] == (False, True)


def test_sync_is_idempotent(db):
    add_policy(db, "p1", [RG_A])
    sync_tenant(db, TENANT); db.commit()
    stats = sync_tenant(db, TENANT); db.commit()

    assert stats["added"] == 0 and stats["removed"] == 0
    assert len(bindings(db)) == 1


def test_existing_manual_binding_is_never_touched(db):
    """Rule 1 — an admin who deliberately exposed the model keeps it
    exposed, and the row must not become reclaimable."""
    db.execute(resource_group_llm_models.insert().values(
        id="manual-1", resource_group_id=RG_A, llm_model_id=JUDGE,
        portal_visible=True, auto_bound=False))
    db.commit()

    add_policy(db, "p1", [RG_A])
    stats = sync_tenant(db, TENANT); db.commit()

    assert stats["added"] == 0 and stats["kept"] == 1
    assert bindings(db)[(RG_A, JUDGE)] == (True, False), "must stay visible + manual"


def test_access_control_change_adds_and_reclaims(db):
    """Rule 3 — moving a policy from group A to B follows the config."""
    add_policy(db, "p1", [RG_A])
    sync_tenant(db, TENANT); db.commit()
    assert (RG_A, JUDGE) in bindings(db)

    unbind_policy_groups(db, "p1")
    db.execute(resource_group_semantic_policies.insert().values(
        resource_group_id=RG_B, semantic_policy_id="p1"))
    db.commit()
    sync_tenant(db, TENANT); db.commit()

    b = bindings(db)
    assert (RG_B, JUDGE) in b, "new group gets the binding"
    assert (RG_A, JUDGE) not in b, "old group's auto-binding is reclaimed"


def test_deleting_policy_reclaims_binding(db):
    add_policy(db, "p1", [RG_A])
    sync_tenant(db, TENANT); db.commit()

    db.execute(resource_group_semantic_policies.delete())
    db.query(SemanticPolicy).delete()
    db.commit()
    stats = sync_tenant(db, TENANT); db.commit()

    assert stats["removed"] == 1
    assert bindings(db) == {}


def test_manual_binding_survives_policy_deletion(db):
    """The dangerous case: teardown must not revoke access an admin set up."""
    db.execute(resource_group_llm_models.insert().values(
        id="manual-1", resource_group_id=RG_A, llm_model_id=JUDGE,
        portal_visible=True, auto_bound=False))
    add_policy(db, "p1", [RG_A])
    sync_tenant(db, TENANT); db.commit()

    db.execute(resource_group_semantic_policies.delete())
    db.query(SemanticPolicy).delete()
    db.commit()
    sync_tenant(db, TENANT); db.commit()

    assert bindings(db)[(RG_A, JUDGE)] == (True, False), "manual binding must survive"


def test_two_policies_sharing_a_group_and_model(db):
    """Refcount trap: deleting one policy must not break the other."""
    add_policy(db, "p1", [RG_A])
    add_policy(db, "p2", [RG_A])
    sync_tenant(db, TENANT); db.commit()
    assert len(bindings(db)) == 1

    db.execute(resource_group_semantic_policies.delete().where(
        resource_group_semantic_policies.c.semantic_policy_id == "p1"))
    db.query(SemanticPolicy).filter(SemanticPolicy.id == "p1").delete()
    db.commit()
    sync_tenant(db, TENANT); db.commit()

    assert (RG_A, JUDGE) in bindings(db), "p2 still needs it"


def test_policy_without_judge_falls_back_to_tenant_default(db):
    db.query(Tenant).filter(Tenant.id == TENANT).update(
        {"default_judge_model_id": OTHER})
    db.commit()
    add_policy(db, "p1", [RG_A], judge_model_id=None)
    sync_tenant(db, TENANT); db.commit()

    assert (RG_A, OTHER) in bindings(db)


def test_unresolvable_judge_binds_nothing(db):
    """No per-policy model and no tenant default → nothing to bind. We do
    NOT bind the runtime's alphabetical last-resort pick."""
    add_policy(db, "p1", [RG_A], judge_model_id=None)
    stats = sync_tenant(db, TENANT); db.commit()

    assert stats["added"] == 0
    assert bindings(db) == {}


# ── semantic cache ─────────────────────────────────────────────────────


def test_cache_config_binds_tenant_embedding_model(db):
    add_cache_config(db, "c1", [RG_A])
    sync_tenant(db, TENANT); db.commit()

    assert bindings(db)[(RG_A, EMBED)] == (False, True)


def test_cache_and_policy_bind_independently(db):
    add_policy(db, "p1", [RG_A])
    add_cache_config(db, "c1", [RG_B])
    sync_tenant(db, TENANT); db.commit()

    b = bindings(db)
    assert (RG_A, JUDGE) in b and (RG_B, EMBED) in b
    assert len(b) == 2


def test_required_set_matches_bindings(db):
    add_policy(db, "p1", [RG_A, RG_B])
    add_cache_config(db, "c1", [RG_A])
    required = compute_required_bindings(db, TENANT)

    assert required == {(RG_A, JUDGE), (RG_B, JUDGE), (RG_A, EMBED)}


# ── knowledge-base collections ─────────────────────────────────────────


def test_kb_collection_binds_tenant_embedding_model_hidden(db):
    """Retrieval embeds the question with the tenant embedding model through
    the governed endpoint. A group with a collection but no binding 403'd
    there and chatted ungrounded, so the binding must follow the collection."""
    add_kb_collection(db, "kb1", [RG_A])
    sync_tenant(db, TENANT); db.commit()

    assert bindings(db)[(RG_A, EMBED)] == (False, True)


def test_unbinding_a_kb_collection_reclaims_the_embedding_binding(db):
    add_kb_collection(db, "kb1", [RG_A])
    sync_tenant(db, TENANT); db.commit()
    db.execute(resource_group_kb_collections.delete().where(
        resource_group_kb_collections.c.kb_collection_id == "kb1"))
    db.commit()
    sync_tenant(db, TENANT); db.commit()

    assert (RG_A, EMBED) not in bindings(db)


def test_kb_and_cache_share_one_embedding_binding(db):
    add_kb_collection(db, "kb1", [RG_A])
    add_cache_config(db, "c1", [RG_A])
    sync_tenant(db, TENANT); db.commit()
    db.execute(resource_group_kb_collections.delete())
    db.commit()
    sync_tenant(db, TENANT); db.commit()

    # The cache still needs it, so it stays.
    assert bindings(db)[(RG_A, EMBED)] == (False, True)
