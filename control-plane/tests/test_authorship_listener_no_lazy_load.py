# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The authorship listener must not query or mutate during a flush

`stamp_resource_authorship` runs in `before_flush`. To decide whether
`updated_by` should move it compares each touched attribute against its
committed value, walking `state.committed_state`.

Reading the current value with `getattr` is the trap. On an attribute that
is not loaded — most often the **backref side of a many-to-many** — that
emits a lazy SELECT in the middle of the flush, and loading a collection
writes into `committed_state`. The listener is iterating that very dict, so
Python raises `RuntimeError: dictionary keys changed during iteration` from
inside `before_flush`, which aborts the transaction.

This hid for an entire release because the listener returns early when no
actor is bound, and nothing that seeded data bound one. The moment seeds
started running as `system` it fired on every boot: the flush aborted
part-way through tenant provisioning, the Default resource group never
landed, and the visible symptom was pages of foreign-key violations naming
a group id that did not exist — nowhere near the real cause.

The shape below is the one that actually broke it: appending from the
*group* side of the association dirties the config's `resource_groups`
backref while that collection is still unloaded on the config.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import Column, ForeignKey, String, Table, create_engine, event
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models.resource_mixins import (
    ResourceAuthorshipMixin,
    ResourceVersionMixin,
    acting_as,
)

# A miniature stand-in for resource_groups ⇄ server_configs. The real
# models carry JSONB columns that SQLite cannot render, and none of that is
# what this test is about — the association plus its backref is.
Base = declarative_base()

_link = Table(
    "mini_group_configs",
    Base.metadata,
    Column("group_id", String(64), ForeignKey("mini_groups.id"), primary_key=True),
    Column("config_id", String(64), ForeignKey("mini_configs.id"), primary_key=True),
)


class MiniGroup(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    __tablename__ = "mini_groups"

    id = Column(String(64), primary_key=True)
    name = Column(String(64))
    configs = relationship(
        "MiniConfig",
        secondary=_link,
        backref="groups",  # ← the unloaded side the listener used to load
    )


class MiniConfig(ResourceAuthorshipMixin, Base):
    __tablename__ = "mini_configs"

    id = Column(String(64), primary_key=True)
    name = Column(String(64))


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _populate(session):
    group = MiniGroup(id="g1", name="Default")
    config = MiniConfig(id="c1", name="cfg")
    session.add_all([group, config])
    session.commit()
    # Fresh identities with nothing but the row loaded, so the association
    # collections are genuinely unloaded — the state the seeds are in.
    session.expunge_all()
    return (
        session.get(MiniGroup, "g1"),
        session.get(MiniConfig, "c1"),
    )


def test_binding_a_config_to_a_group_does_not_abort_the_flush(session):
    group, config = _populate(session)

    with acting_as("system"):
        group.configs.append(config)
        # Before the fix this raised RuntimeError from before_flush.
        session.flush()

    session.commit()
    assert config in group.configs


def test_the_backref_side_still_gets_stamped(session):
    group, config = _populate(session)

    with acting_as("system"):
        group.configs.append(config)
        session.flush()

    # The association itself is a real change to both rows, so both are
    # attributable. Swallowing the error and skipping the stamp would have
    # been the wrong repair.
    assert group.updated_by == "system"


def test_the_listener_emits_no_sql_of_its_own(session):
    """The listener must decide from what is already in memory.

    A query from inside `before_flush` is the defect this guards, in its
    general form: the lazy load was only the way it surfaced. Anything the
    listener triggers here runs at the worst possible moment and can
    reorder or abort the flush.
    """
    group, config = _populate(session)

    statements: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with acting_as("system"):
        group.configs.append(config)
        event.listen(session.bind, "before_cursor_execute", record)
        try:
            session.flush()
        finally:
            event.remove(session.bind, "before_cursor_execute", record)

    # The flush legitimately writes. What it must not do is SELECT — that
    # would be the listener going to the database to answer a question it
    # should not be asking.
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects == [], f"listener triggered reads during flush: {selects}"
