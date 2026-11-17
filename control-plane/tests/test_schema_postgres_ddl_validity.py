# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Schema guards for the FRESH-database path.

A Brutor database is built one of two ways (see `app/database/__init__.py`):

  * **fresh**  — `Base.metadata.create_all()` from the ORM models, then
    stamped at Alembic head;
  * **existing** — `alembic upgrade head`, from the migration chain.

Those are two independent descriptions of the same schema, and only the second
one gets exercised by the migration tests. When they drift, the fresh path is
the one that breaks — and it breaks on a brand-new install, which is the worst
possible audience for it.

That is exactly what happened in 0.10.34: `ix_agent_cards_extensions_gin` was
added to `AgentCard.__table_args__`, but `extensions` was declared as plain
`JSON` in the model while migration a012_a2a_v1_schema creates it as `JSONB`. Postgres has no
default GIN operator class for `json`, so every fresh install died at startup
with:

    data type json has no default operator class for access method "gin"

while every migrated install stayed healthy. These tests pin the invariants
that would have caught it, without needing a live database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ARRAY, JSON, Text
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY, JSONB, TSVECTOR

from app.database import Base
import app.database.models  # noqa: F401  — registers every model on Base


def _gin_indexes():
    """Every Index in the metadata declared `postgresql_using="gin"`."""
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            using = index.dialect_options.get("postgresql", {}).get("using")
            if using == "gin":
                yield table, index


def test_gin_indexes_are_on_gin_indexable_types():
    """A GIN index must sit on a type Postgres can actually GIN-index.

    Postgres ships default GIN operator classes for `jsonb`, array types and
    `tsvector` — but **not** for `json`. Indexing a `json` column raises
    UndefinedObject at CREATE INDEX time, which on the fresh path means the
    whole `create_all()` transaction rolls back and the service never starts.
    """
    indexable = (JSONB, TSVECTOR, ARRAY, PG_ARRAY)
    offenders = []

    for table, index in _gin_indexes():
        for col in index.columns:
            if isinstance(col.type, indexable):
                continue
            # `json` is the specific trap: it looks right and fails at runtime.
            offenders.append(
                f"{table.name}.{col.name} is {type(col.type).__name__} "
                f"(index {index.name})"
            )

    assert not offenders, (
        "GIN index declared on a column Postgres cannot GIN-index — "
        "`create_all()` will fail on every FRESH database while migrated "
        "databases stay healthy. Use JSONB (not JSON) for indexed documents:\n  "
        + "\n  ".join(offenders)
    )


def test_at_least_one_gin_index_is_declared():
    """Guards the guard.

    If a refactor ever stops declaring GIN indexes on the models — or moves
    them somewhere `_gin_indexes()` can't see — the test above would pass
    vacuously and the invariant would quietly stop being enforced.
    """
    assert list(_gin_indexes()), (
        "No GIN indexes found in the model metadata. Either they moved out of "
        "`__table_args__`, or this guard is no longer looking in the right "
        "place — check before deleting it."
    )


@pytest.mark.parametrize(
    "table_name,column_name",
    [
        # agent_cards / agent_card_capabilities — migration a012_a2a_v1_schema (JSONB).
        # `extensions` additionally carries the GIN index above.
        ("agent_cards", "extensions"),
        ("agent_cards", "signatures"),
        ("agent_cards", "supported_interfaces"),
        ("agent_cards", "extended_card_json"),
        ("agent_card_capabilities", "extensions"),
        # Columns whose migrations create JSONB but whose models once said
        # JSON. None were indexed, so none had broken yet — the
        # next GIN index or `@>` containment query on any of them would have
        # reproduced the 0.10.34 outage.
        ("a2a_tasks", "task_json"),
        ("argument_policies", "analyzer_config"),
        ("argument_policies", "rules"),
        ("kb_connectors", "config"),
        ("llm_model", "fallback_models"),
        ("llm_model", "supported_image_qualities"),
        ("llm_model", "supported_image_sizes"),
        ("llm_model", "supported_response_formats"),
        ("tenants", "theme_variants"),
    ],
)
def test_migration_jsonb_columns_are_jsonb_in_the_model(table_name, column_name):
    """Model type must match what the migration chain writes.

    Each column here is created as `postgresql.JSONB()` by its migration. If
    the model says plain `JSON`, a fresh install gets `json` and a migrated
    install gets `jsonb` — the same release running two different schemas,
    which surfaces later as an index that won't build or an operator that
    won't resolve.
    """
    table = Base.metadata.tables[table_name]
    col = table.c[column_name]
    assert isinstance(col.type, JSONB), (
        f"{table_name}.{column_name} is {type(col.type).__name__} in the model "
        f"but JSONB in its migration. Fresh and migrated databases would "
        f"disagree on this column's type."
    )
    # Belt and braces: JSONB subclasses JSON in SQLAlchemy, so an
    # isinstance(JSON) check would pass for both. Pin the rendered DDL.
    assert col.type.compile(dialect=_pg_dialect()) == "JSONB"


def _pg_dialect():
    from sqlalchemy.dialects import postgresql

    return postgresql.dialect()
