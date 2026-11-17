# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Test support for the edition marker, the Enterprise-data check and the reset
command.

* `community_composition()` and `become_community_process()`: re-exported
  from `tests/community_process.py` (which a subprocess can import without
  this module's test-suite environment).
* `scratch_databases()`: an empty schema (every model's table) on SQLite, and
  on a throwaway Postgres database when `BRUTOR_PG_TESTS=1`. The Postgres
  database is created by the test run and dropped after it; nothing here
  touches a database the run did not create.
* `insert_row()`: one row in any table, with a value for every column that
  needs one. Foreign keys are not checked while it inserts (the tests put
  single rows into tables whose parents they do not need).
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterator, Optional

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

import tests.conftest  # noqa: F401  (registers the SQLite JSONB shims)

PG_DATABASE_PREFIX = "brutor_control_d15"

from tests.community_process import become_community_process, community_composition  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Scratch databases
# ---------------------------------------------------------------------------


def pg_enabled() -> bool:
    return os.environ.get("BRUTOR_PG_TESTS") == "1"


def _create_schema(engine: Engine) -> None:
    import app.database.models  # noqa: F401  (registers every model)
    from app.database import Base

    Base.metadata.create_all(bind=engine)


@contextmanager
def scratch_database(dialect: str, tmp_path_factory) -> Iterator[Engine]:
    """An engine on an empty schema of every model's table."""
    if dialect == "sqlite":
        path = tmp_path_factory.mktemp("d15") / "edition.sqlite"
        engine = create_engine(f"sqlite:///{path}", future=True)
        _create_schema(engine)
        try:
            yield engine
        finally:
            engine.dispose()
        return

    with scratch_pg_database() as url:
        engine = create_engine(url, future=True)
        _create_schema(engine)
        try:
            yield engine
        finally:
            engine.dispose()


@contextmanager
def scratch_pg_database() -> Iterator[str]:
    """The URL of an EMPTY Postgres database, created on `BRUTOR_PG_TEST_URL`'s
    server for the caller (named `brutor_control_d15_...`) and dropped
    afterwards."""
    from tests.admin_endpoint_harness import throwaway_pg_database

    for url in throwaway_pg_database(PG_DATABASE_PREFIX):
        assert url is not None and PG_DATABASE_PREFIX in url
        yield url


def empty_every_table(engine: Engine) -> None:
    """Delete every row of the scratch database (never a database the test
    run did not create: the engine comes from `scratch_database`)."""
    from app.database import Base

    assert _is_scratch(engine), engine.url
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE TABLE {names} CASCADE"))
        else:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())


def _is_scratch(engine: Engine) -> bool:
    name = engine.url.database or ""
    return PG_DATABASE_PREFIX in name or name.endswith("edition.sqlite")


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _value_for(column: sa.Column) -> Any:
    kind = column.type
    if isinstance(kind, sa.Enum):
        return kind.enums[0]
    try:
        python_type = kind.python_type
    except NotImplementedError:
        python_type = str
    if python_type is str:
        length = getattr(kind, "length", None) or 32
        return uuid.uuid4().hex[: min(length, 32)]
    if python_type is int:
        return 1
    if python_type is bool:
        return False
    if python_type is float:
        return 1.0
    if python_type is Decimal:
        return Decimal("1")
    if python_type is datetime:
        return datetime.now(timezone.utc)
    if python_type is date:
        return date.today()
    if python_type is dict:
        return {}
    if python_type is list:
        return []
    if python_type is bytes:
        return b"x"
    if python_type is uuid.UUID:
        return uuid.uuid4()
    return uuid.uuid4().hex[:16]


# Columns a CHECK constraint holds to a list.
_ROW_VALUES = {
    "kb_connectors": {"connector_type": "confluence"},
    "kb_connector_identity_mappings": {"connector_type": "confluence"},
}


def community_feature_flags() -> Dict[str, bool]:
    """`tenants.features` as a Community Edition leaves it."""
    from app.composition.edition import ENTERPRISE_TENANT_FEATURE_FLAGS
    from app.constants.tenant import TENANT_FEATURE_KEYS

    return {key: key not in ENTERPRISE_TENANT_FEATURE_FLAGS for key in TENANT_FEATURE_KEYS}


def insert_tenant(conn: Connection, tenant_id: str = "default", **values: Any) -> Dict[str, Any]:
    values.setdefault("features", community_feature_flags())
    values.setdefault("is_default", tenant_id == "default")
    return insert_row(conn, "tenants", id=tenant_id, name=tenant_id, **values)


def insert_row(conn: Connection, table_name: str, **values: Any) -> Dict[str, Any]:
    """Insert one row into `table_name`; returns the values written."""
    import app.database.models  # noqa: F401  (registers every model)
    from app.database import Base

    table = Base.metadata.tables[table_name]
    values = {**_ROW_VALUES.get(table_name, {}), **values}
    row: Dict[str, Any] = {}
    for column in table.columns:
        if column.name in values:
            row[column.name] = values[column.name]
            continue
        if column.name == "tenant_id":
            row[column.name] = "default"
            continue
        needs_value = column.primary_key or not column.nullable
        has_default = column.default is not None or column.server_default is not None
        if column.primary_key and column.autoincrement is True and isinstance(column.type, sa.Integer):
            continue
        if needs_value and not has_default:
            row[column.name] = _value_for(column)
    if conn.dialect.name == "postgresql":
        # Single rows, without their parents: no foreign-key triggers.
        conn.execute(text("SET LOCAL session_replication_role = replica"))
    conn.execute(table.insert().values(**row))
    return row


def count(conn: Connection, table_name: str, where: Optional[str] = None) -> int:
    sql = f'SELECT count(*) FROM "{table_name}"' + (f" WHERE {where}" if where else "")
    return int(conn.execute(text(sql)).scalar())
